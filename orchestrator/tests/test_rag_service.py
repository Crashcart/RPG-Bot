"""
Unit tests for orchestrator/services/rag_service.py

Covers:
- RAGService.connect() and .client property
- retrieve_rule_chunks(): single/multi collection, relevance sort, top-N cap, failure skip
- ingest_document(): chunk add + return count
- Edge cases: no collections, n_results limiting, all collections fail
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.rag_service import RAGService


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_settings(**kwargs):
    s = MagicMock()
    s.chroma_host = kwargs.get("chroma_host", "localhost")
    s.chroma_port = kwargs.get("chroma_port", 8000)
    return s


def _make_chroma_results(docs, metas, distances):
    """Build a minimal ChromaDB query result dict."""
    return {
        "documents": [docs],
        "metadatas": [metas],
        "distances": [distances],
    }


# ── TestConnect ───────────────────────────────────────────────────────────────

class TestConnect:
    @pytest.mark.asyncio
    async def test_connect_sets_client(self):
        svc = RAGService(_make_settings())
        mock_client = AsyncMock()
        with patch("orchestrator.services.rag_service.chromadb.AsyncHttpClient", return_value=mock_client):
            await svc.connect()
        assert svc._client is mock_client

    def test_client_property_raises_before_connect(self):
        svc = RAGService(_make_settings())
        with pytest.raises(RuntimeError, match="not connected"):
            _ = svc.client

    @pytest.mark.asyncio
    async def test_client_property_returns_after_connect(self):
        svc = RAGService(_make_settings())
        mock_client = AsyncMock()
        with patch("orchestrator.services.rag_service.chromadb.AsyncHttpClient", return_value=mock_client):
            await svc.connect()
        assert svc.client is mock_client


# ── TestRetrieveRuleChunks ────────────────────────────────────────────────────

class TestRetrieveRuleChunks:
    def _make_service(self):
        svc = RAGService(_make_settings())
        svc._client = AsyncMock()
        return svc

    @pytest.mark.asyncio
    async def test_single_collection_returns_chunks(self):
        svc = self._make_service()
        mock_col = AsyncMock()
        mock_col.query = AsyncMock(return_value=_make_chroma_results(
            docs=["Rule text A", "Rule text B"],
            metas=[
                {"chunk_id": "c1", "source": "PHB p.10"},
                {"chunk_id": "c2", "source": "PHB p.11"},
            ],
            distances=[0.1, 0.3],
        ))
        svc._client.get_collection = AsyncMock(return_value=mock_col)

        chunks = await svc.retrieve_rule_chunks("attack roll", ["collection_a"])

        assert len(chunks) == 2
        assert chunks[0].chunk_id == "c1"
        assert chunks[0].source == "PHB p.10"
        assert chunks[0].content == "Rule text A"
        # relevance = 1.0 - distance
        assert abs(chunks[0].relevance - 0.9) < 0.001
        assert abs(chunks[1].relevance - 0.7) < 0.001

    @pytest.mark.asyncio
    async def test_multi_collection_merged_and_sorted(self):
        svc = self._make_service()

        col_a = AsyncMock()
        col_a.query = AsyncMock(return_value=_make_chroma_results(
            docs=["Low relevance doc"],
            metas=[{"chunk_id": "a1", "source": "A"}],
            distances=[0.8],
        ))
        col_b = AsyncMock()
        col_b.query = AsyncMock(return_value=_make_chroma_results(
            docs=["High relevance doc"],
            metas=[{"chunk_id": "b1", "source": "B"}],
            distances=[0.1],
        ))

        async def _get_collection(name):
            return col_a if name == "col_a" else col_b

        svc._client.get_collection = AsyncMock(side_effect=_get_collection)

        chunks = await svc.retrieve_rule_chunks("query", ["col_a", "col_b"], n_results=5)

        # highest relevance (col_b, dist=0.1) should come first
        assert chunks[0].chunk_id == "b1"
        assert chunks[1].chunk_id == "a1"

    @pytest.mark.asyncio
    async def test_top_n_cap_applied_across_collections(self):
        svc = self._make_service()

        col = AsyncMock()
        col.query = AsyncMock(return_value=_make_chroma_results(
            docs=[f"doc{i}" for i in range(5)],
            metas=[{"chunk_id": f"c{i}", "source": "S"} for i in range(5)],
            distances=[0.1 * i for i in range(5)],
        ))
        svc._client.get_collection = AsyncMock(return_value=col)

        chunks = await svc.retrieve_rule_chunks("query", ["col_a", "col_b"], n_results=3)
        assert len(chunks) <= 3

    @pytest.mark.asyncio
    async def test_failed_collection_skipped_others_returned(self):
        svc = self._make_service()

        good_col = AsyncMock()
        good_col.query = AsyncMock(return_value=_make_chroma_results(
            docs=["Good doc"],
            metas=[{"chunk_id": "g1", "source": "Good"}],
            distances=[0.2],
        ))

        async def _get_collection(name):
            if name == "bad_col":
                raise Exception("collection not found")
            return good_col

        svc._client.get_collection = AsyncMock(side_effect=_get_collection)

        chunks = await svc.retrieve_rule_chunks("query", ["bad_col", "good_col"])
        assert len(chunks) == 1
        assert chunks[0].chunk_id == "g1"

    @pytest.mark.asyncio
    async def test_all_collections_fail_returns_empty(self):
        svc = self._make_service()
        svc._client.get_collection = AsyncMock(side_effect=Exception("chroma down"))

        chunks = await svc.retrieve_rule_chunks("query", ["col_a", "col_b"])
        assert chunks == []

    @pytest.mark.asyncio
    async def test_empty_collection_list_returns_empty(self):
        svc = self._make_service()
        chunks = await svc.retrieve_rule_chunks("query", [])
        assert chunks == []
        svc._client.get_collection.assert_not_called()

    @pytest.mark.asyncio
    async def test_negative_distance_clamped_to_zero_relevance(self):
        """ChromaDB can return slightly negative distances; relevance must not go below 0."""
        svc = self._make_service()
        col = AsyncMock()
        col.query = AsyncMock(return_value=_make_chroma_results(
            docs=["doc"],
            metas=[{"chunk_id": "x", "source": "S"}],
            distances=[1.5],  # distance > 1 → relevance would be negative
        ))
        svc._client.get_collection = AsyncMock(return_value=col)

        chunks = await svc.retrieve_rule_chunks("q", ["col"])
        assert chunks[0].relevance == 0.0

    @pytest.mark.asyncio
    async def test_missing_meta_fields_use_defaults(self):
        """Missing chunk_id/source in metadata should not crash."""
        svc = self._make_service()
        col = AsyncMock()
        col.query = AsyncMock(return_value=_make_chroma_results(
            docs=["text"],
            metas=[{}],          # no chunk_id, no source
            distances=[0.5],
        ))
        svc._client.get_collection = AsyncMock(return_value=col)

        chunks = await svc.retrieve_rule_chunks("q", ["col"])
        assert len(chunks) == 1
        assert chunks[0].chunk_id == "unknown"

    @pytest.mark.asyncio
    async def test_relevance_rounded_to_4_decimals(self):
        svc = self._make_service()
        col = AsyncMock()
        col.query = AsyncMock(return_value=_make_chroma_results(
            docs=["doc"],
            metas=[{"chunk_id": "r1", "source": "S"}],
            distances=[0.123456789],
        ))
        svc._client.get_collection = AsyncMock(return_value=col)

        chunks = await svc.retrieve_rule_chunks("q", ["col"])
        # relevance = round(1.0 - 0.123456789, 4) = 0.8765
        assert chunks[0].relevance == round(1.0 - 0.123456789, 4)


# ── TestIngestDocument ────────────────────────────────────────────────────────

class TestIngestDocument:
    def _make_service(self):
        svc = RAGService(_make_settings())
        svc._client = AsyncMock()
        return svc

    @pytest.mark.asyncio
    async def test_ingest_returns_chunk_count(self):
        svc = self._make_service()
        mock_col = AsyncMock()
        svc._client.get_or_create_collection = AsyncMock(return_value=mock_col)

        chunks = [
            {"id": f"c{i}", "text": f"text {i}", "source": "PHB"}
            for i in range(4)
        ]
        count = await svc.ingest_document("test_collection", chunks)

        assert count == 4

    @pytest.mark.asyncio
    async def test_ingest_calls_collection_add_with_correct_args(self):
        svc = self._make_service()
        mock_col = AsyncMock()
        svc._client.get_or_create_collection = AsyncMock(return_value=mock_col)

        chunks = [{"id": "c1", "text": "damage rules", "source": "DMG p.42"}]
        await svc.ingest_document("rules", chunks)

        mock_col.add.assert_called_once_with(
            ids=["c1"],
            documents=["damage rules"],
            metadatas=[{"source": "DMG p.42", "chunk_id": "c1"}],
        )

    @pytest.mark.asyncio
    async def test_ingest_empty_list_returns_zero(self):
        svc = self._make_service()
        mock_col = AsyncMock()
        svc._client.get_or_create_collection = AsyncMock(return_value=mock_col)

        count = await svc.ingest_document("col", [])
        assert count == 0
