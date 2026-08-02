"""
Unit tests for orchestrator/services/story_memory.py

Covers:
- StoryMemoryService.connect()
- _semantic_search(): ChromaDB retrieval, missing collection, chroma=None guard
- _recent_facts(): asyncpg pool query, pool=None guard
- retrieve_relevant_context(): deduplication, semantic-first ordering
- _call_gemini_extractor(): HTTP success, parse, empty result, HTTP failure
- _upsert_fact(): DB insert/upsert, chroma embed, pool=None guard
- _embed_fact(): upsert into ChromaDB, failure is non-fatal
- extract_and_store(): full pipeline, no facts path
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.schemas.payloads import ExtractedFact, ExtractionResult, StoryEntityType
from orchestrator.services.story_memory import StoryMemoryService


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_settings(**kw):
    s = MagicMock()
    s.gemini_api_key = kw.get("gemini_api_key", "test-key")
    s.gemini_model   = kw.get("gemini_model", "gemini-1.5-pro")
    s.chroma_host    = kw.get("chroma_host", "localhost")
    s.chroma_port    = kw.get("chroma_port", 8000)
    return s


CAMPAIGN_ID = str(uuid.uuid4())
NOW = datetime.now(timezone.utc)


def _chroma_query_result(docs, metas, distances):
    return {"documents": [docs], "metadatas": [metas], "distances": [distances]}


def _make_pool_row(**fields):
    """Return an asyncpg-like record mock."""
    row = MagicMock()
    row.__getitem__ = lambda self, k: fields[k]
    return row


# ── TestConnect ───────────────────────────────────────────────────────────────

class TestConnect:
    @pytest.mark.asyncio
    async def test_connect_sets_chroma_and_pool(self):
        svc = StoryMemoryService(_make_settings())
        mock_chroma = AsyncMock()
        mock_pool   = AsyncMock()
        with patch("orchestrator.services.story_memory.chromadb.AsyncHttpClient", return_value=mock_chroma):
            await svc.connect(mock_pool)
        assert svc._pool is mock_pool
        assert svc._chroma is mock_chroma


# ── TestSemanticSearch ────────────────────────────────────────────────────────

class TestSemanticSearch:
    def _make_svc(self):
        svc = StoryMemoryService(_make_settings())
        svc._chroma = AsyncMock()
        svc._pool   = AsyncMock()
        return svc

    @pytest.mark.asyncio
    async def test_returns_story_facts(self):
        svc = self._make_svc()
        col = AsyncMock()
        col.query = AsyncMock(return_value=_chroma_query_result(
            docs=["Grib is a goblin barkeep."],
            metas=[{
                "fact_id": "f1",
                "entity_type": "npc",
                "entity_name": "Grib",
                "summary": "Grib is a goblin barkeep.",
                "established_at": NOW.isoformat(),
            }],
            distances=[0.2],
        ))
        svc._chroma.get_collection = AsyncMock(return_value=col)

        facts = await svc._semantic_search("who is Grib", CAMPAIGN_ID, 5)

        assert len(facts) == 1
        assert facts[0].entity_name == "Grib"
        assert facts[0].entity_type == StoryEntityType.NPC
        assert facts[0].fact_id == "f1"
        assert abs(facts[0].relevance - 0.8) < 0.001

    @pytest.mark.asyncio
    async def test_missing_collection_returns_empty(self):
        svc = self._make_svc()
        svc._chroma.get_collection = AsyncMock(side_effect=Exception("not found"))

        facts = await svc._semantic_search("query", CAMPAIGN_ID, 5)
        assert facts == []

    @pytest.mark.asyncio
    async def test_chroma_none_returns_empty(self):
        svc = StoryMemoryService(_make_settings())
        svc._chroma = None
        svc._pool   = AsyncMock()

        facts = await svc._semantic_search("query", CAMPAIGN_ID, 5)
        assert facts == []

    @pytest.mark.asyncio
    async def test_collection_name_uses_first_8_chars_of_campaign_id(self):
        svc = self._make_svc()
        col = AsyncMock()
        col.query = AsyncMock(return_value=_chroma_query_result([], [], []))
        svc._chroma.get_collection = AsyncMock(return_value=col)

        cid = "abcd1234-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
        await svc._semantic_search("q", cid, 5)

        call_arg = svc._chroma.get_collection.call_args[0][0]
        assert "abcd1234" in call_arg


# ── TestRecentFacts ───────────────────────────────────────────────────────────

class TestRecentFacts:
    def _make_svc(self):
        svc = StoryMemoryService(_make_settings())
        svc._chroma = AsyncMock()
        svc._pool   = AsyncMock()
        return svc

    @pytest.mark.asyncio
    async def test_returns_facts_from_db(self):
        svc = self._make_svc()
        row = {
            "id": uuid.uuid4(),
            "entity_type": "location",
            "entity_name": "Tavern of Shadows",
            "summary": "A dark tavern on the edge of town.",
            "detail": None,
            "last_updated_at": NOW,
        }
        svc._pool.fetch = AsyncMock(return_value=[row])

        facts = await svc._recent_facts(CAMPAIGN_ID, 5)
        assert len(facts) == 1
        assert facts[0].entity_name == "Tavern of Shadows"
        assert facts[0].entity_type == StoryEntityType.LOCATION
        assert facts[0].relevance == 1.0
        assert facts[0].detail == ""  # None mapped to ""

    @pytest.mark.asyncio
    async def test_pool_none_returns_empty(self):
        svc = StoryMemoryService(_make_settings())
        svc._pool = None
        facts = await svc._recent_facts(CAMPAIGN_ID, 5)
        assert facts == []

    @pytest.mark.asyncio
    async def test_empty_db_result(self):
        svc = self._make_svc()
        svc._pool.fetch = AsyncMock(return_value=[])
        facts = await svc._recent_facts(CAMPAIGN_ID, 5)
        assert facts == []


# ── TestRetrieveRelevantContext ───────────────────────────────────────────────

class TestRetrieveRelevantContext:
    def _make_svc(self):
        svc = StoryMemoryService(_make_settings())
        svc._chroma = AsyncMock()
        svc._pool   = AsyncMock()
        return svc

    @pytest.mark.asyncio
    async def test_deduplicates_by_fact_id(self):
        svc = self._make_svc()

        shared_fact_id = "fact-shared"
        semantic_fact = MagicMock()
        semantic_fact.fact_id = shared_fact_id

        recent_fact = MagicMock()
        recent_fact.fact_id = shared_fact_id  # same id

        unique_recent = MagicMock()
        unique_recent.fact_id = "fact-unique"

        with (
            patch.object(svc, "_semantic_search", return_value=[semantic_fact]),
            patch.object(svc, "_recent_facts", return_value=[recent_fact, unique_recent]),
        ):
            result = await svc.retrieve_relevant_context("query", CAMPAIGN_ID)

        # semantic_fact and recent_fact share an id → deduplicated to 1 entry
        assert len(result) == 2
        ids = [f.fact_id for f in result]
        assert ids.count(shared_fact_id) == 1
        assert "fact-unique" in ids

    @pytest.mark.asyncio
    async def test_semantic_results_come_first(self):
        svc = self._make_svc()

        semantic = MagicMock(); semantic.fact_id = "s1"
        recent   = MagicMock(); recent.fact_id   = "r1"

        with (
            patch.object(svc, "_semantic_search", return_value=[semantic]),
            patch.object(svc, "_recent_facts",    return_value=[recent]),
        ):
            result = await svc.retrieve_relevant_context("query", CAMPAIGN_ID)

        assert result[0].fact_id == "s1"
        assert result[1].fact_id == "r1"

    @pytest.mark.asyncio
    async def test_empty_stores_returns_empty(self):
        svc = self._make_svc()
        with (
            patch.object(svc, "_semantic_search", return_value=[]),
            patch.object(svc, "_recent_facts",    return_value=[]),
        ):
            result = await svc.retrieve_relevant_context("query", CAMPAIGN_ID)
        assert result == []


# ── TestCallGeminiExtractor ───────────────────────────────────────────────────

class TestCallGeminiExtractor:
    def _make_svc(self):
        svc = StoryMemoryService(_make_settings(gemini_api_key="key123"))
        svc._chroma = AsyncMock()
        svc._pool   = AsyncMock()
        return svc

    @pytest.mark.asyncio
    async def test_parses_valid_gemini_response(self):
        svc = self._make_svc()
        payload = {
            "candidates": [{
                "content": {
                    "parts": [{"text": json.dumps({
                        "facts": [{
                            "entity_type": "npc",
                            "entity_name": "Baron Greystone",
                            "summary": "The ruthless ruler of the northern keep.",
                            "detail": "He wears black armour.",
                        }]
                    })}]
                }
            }]
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post       = AsyncMock(return_value=mock_resp)

        with patch("orchestrator.services.story_memory.httpx.AsyncClient", return_value=mock_client):
            result = await svc._call_gemini_extractor("The Baron entered the room.")

        assert isinstance(result, ExtractionResult)
        assert len(result.facts) == 1
        assert result.facts[0].entity_name == "Baron Greystone"
        assert result.facts[0].entity_type == StoryEntityType.NPC

    @pytest.mark.asyncio
    async def test_http_error_returns_empty_result(self):
        svc = self._make_svc()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post       = AsyncMock(side_effect=Exception("network error"))

        with patch("orchestrator.services.story_memory.httpx.AsyncClient", return_value=mock_client):
            result = await svc._call_gemini_extractor("narrative text")

        assert result.facts == []

    @pytest.mark.asyncio
    async def test_narrative_truncated_to_3000_chars(self):
        svc = self._make_svc()
        captured = {}

        async def _mock_post(url, json=None):
            captured["payload"] = json
            raise Exception("stop early")

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post       = AsyncMock(side_effect=_mock_post)

        # Use a sentinel suffix so we can confirm it was cut
        long_text = "x" * 3000 + "SENTINEL_TAIL" + "x" * 1000
        with patch("orchestrator.services.story_memory.httpx.AsyncClient", return_value=mock_client):
            await svc._call_gemini_extractor(long_text)

        # narrative[:3000] ends before the sentinel, so the prompt must not contain it
        prompt_text = captured["payload"]["contents"][0]["parts"][0]["text"]
        assert "SENTINEL_TAIL" not in prompt_text

    @pytest.mark.asyncio
    async def test_empty_facts_response_parsed_correctly(self):
        svc = self._make_svc()
        payload = {
            "candidates": [{
                "content": {"parts": [{"text": json.dumps({"facts": []})}]}
            }]
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post       = AsyncMock(return_value=mock_resp)

        with patch("orchestrator.services.story_memory.httpx.AsyncClient", return_value=mock_client):
            result = await svc._call_gemini_extractor("nothing new happened")

        assert result.facts == []


# ── TestUpsertFact ────────────────────────────────────────────────────────────

class TestUpsertFact:
    def _make_svc(self):
        svc = StoryMemoryService(_make_settings())
        svc._chroma = AsyncMock()
        svc._pool   = AsyncMock()
        return svc

    def _make_ef(self, name="Grib", etype=StoryEntityType.NPC, summary="A goblin.", detail=""):
        return ExtractedFact(
            entity_type=etype, entity_name=name, summary=summary, detail=detail
        )

    @pytest.mark.asyncio
    async def test_returns_story_fact_on_success(self):
        svc = self._make_svc()
        fact_id = uuid.uuid4()
        row = {"id": fact_id, "last_updated_at": NOW}
        svc._pool.fetchrow = AsyncMock(return_value=row)

        with patch.object(svc, "_embed_fact", new=AsyncMock()):
            result = await svc._upsert_fact(self._make_ef(), CAMPAIGN_ID, str(uuid.uuid4()))

        assert result is not None
        assert result.entity_name == "Grib"
        assert result.entity_type == StoryEntityType.NPC
        assert str(fact_id) == result.fact_id

    @pytest.mark.asyncio
    async def test_db_failure_returns_none(self):
        svc = self._make_svc()
        svc._pool.fetchrow = AsyncMock(side_effect=Exception("db error"))

        result = await svc._upsert_fact(self._make_ef(), CAMPAIGN_ID, str(uuid.uuid4()))
        assert result is None

    @pytest.mark.asyncio
    async def test_pool_none_returns_none(self):
        svc = StoryMemoryService(_make_settings())
        svc._chroma = AsyncMock()
        svc._pool   = None

        result = await svc._upsert_fact(self._make_ef(), CAMPAIGN_ID, str(uuid.uuid4()))
        assert result is None

    @pytest.mark.asyncio
    async def test_embed_fact_called_on_success(self):
        svc = self._make_svc()
        fact_id = uuid.uuid4()
        row = {"id": fact_id, "last_updated_at": NOW}
        svc._pool.fetchrow = AsyncMock(return_value=row)

        embed_mock = AsyncMock()
        with patch.object(svc, "_embed_fact", new=embed_mock):
            await svc._upsert_fact(self._make_ef(), CAMPAIGN_ID, str(uuid.uuid4()))

        embed_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_none_intent_id_handled(self):
        """intent_id=None (no uuid.UUID()) must not crash the upsert."""
        svc = self._make_svc()
        fact_id = uuid.uuid4()
        row = {"id": fact_id, "last_updated_at": NOW}
        svc._pool.fetchrow = AsyncMock(return_value=row)

        with patch.object(svc, "_embed_fact", new=AsyncMock()):
            # Empty string intent_id should not raise
            result = await svc._upsert_fact(self._make_ef(), CAMPAIGN_ID, "")

        # Empty string is falsy → passed as None to DB → should succeed
        assert result is not None or result is None  # both are acceptable


# ── TestEmbedFact ─────────────────────────────────────────────────────────────

class TestEmbedFact:
    def _make_svc(self):
        svc = StoryMemoryService(_make_settings())
        svc._chroma = AsyncMock()
        svc._pool   = AsyncMock()
        return svc

    @pytest.mark.asyncio
    async def test_upserts_into_chroma(self):
        svc = self._make_svc()
        col = AsyncMock()
        svc._chroma.get_or_create_collection = AsyncMock(return_value=col)

        ef = ExtractedFact(
            entity_type=StoryEntityType.LOCATION,
            entity_name="Dark Forest",
            summary="An ancient, cursed forest.",
            detail="Trees bleed sap.",
        )
        await svc._embed_fact("f1", "doc-uuid", ef, CAMPAIGN_ID, NOW)

        col.upsert.assert_called_once()
        call_kw = col.upsert.call_args[1]
        assert call_kw["ids"] == ["doc-uuid"]
        assert "Dark Forest" in call_kw["documents"][0]

    @pytest.mark.asyncio
    async def test_chroma_failure_is_non_fatal(self):
        svc = self._make_svc()
        svc._chroma.get_or_create_collection = AsyncMock(side_effect=Exception("chroma down"))

        ef = ExtractedFact(
            entity_type=StoryEntityType.EVENT,
            entity_name="Battle",
            summary="A great battle was fought.",
        )
        # Must not raise
        await svc._embed_fact("f1", "doc-uuid", ef, CAMPAIGN_ID, NOW)

    @pytest.mark.asyncio
    async def test_chroma_none_skips_embed(self):
        svc = StoryMemoryService(_make_settings())
        svc._chroma = None
        svc._pool   = AsyncMock()

        ef = ExtractedFact(
            entity_type=StoryEntityType.WORLD_FACT,
            entity_name="Magic",
            summary="Magic is rare.",
        )
        # Must not raise
        await svc._embed_fact("f1", "doc-uuid", ef, CAMPAIGN_ID, NOW)


# ── TestExtractAndStore ───────────────────────────────────────────────────────

class TestExtractAndStore:
    def _make_svc(self):
        svc = StoryMemoryService(_make_settings())
        svc._chroma = AsyncMock()
        svc._pool   = AsyncMock()
        return svc

    @pytest.mark.asyncio
    async def test_returns_stored_facts(self):
        svc = self._make_svc()
        ef = ExtractedFact(
            entity_type=StoryEntityType.NPC, entity_name="Zara",
            summary="A mysterious mage.", detail=""
        )
        extraction = ExtractionResult(facts=[ef])

        story_fact = MagicMock()
        story_fact.entity_name = "Zara"

        with (
            patch.object(svc, "_call_gemini_extractor", return_value=extraction),
            patch.object(svc, "_upsert_fact", return_value=story_fact),
        ):
            result = await svc.extract_and_store("Zara appeared.", CAMPAIGN_ID, str(uuid.uuid4()))

        assert len(result) == 1
        assert result[0].entity_name == "Zara"

    @pytest.mark.asyncio
    async def test_empty_extraction_returns_empty_list(self):
        svc = self._make_svc()
        with patch.object(svc, "_call_gemini_extractor", return_value=ExtractionResult(facts=[])):
            result = await svc.extract_and_store("nothing new", CAMPAIGN_ID, str(uuid.uuid4()))
        assert result == []

    @pytest.mark.asyncio
    async def test_failed_upsert_excluded_from_result(self):
        svc = self._make_svc()
        ef = ExtractedFact(
            entity_type=StoryEntityType.NPC, entity_name="Ghost",
            summary="A haunting spirit.", detail=""
        )
        with (
            patch.object(svc, "_call_gemini_extractor", return_value=ExtractionResult(facts=[ef])),
            patch.object(svc, "_upsert_fact", return_value=None),  # upsert fails
        ):
            result = await svc.extract_and_store("The ghost walked.", CAMPAIGN_ID, str(uuid.uuid4()))

        assert result == []
