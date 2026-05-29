"""Tests for PDFProcessorService — chunking logic and ingestion pipeline."""
from __future__ import annotations

import pathlib
import tempfile

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.services.pdf_processor import PDFProcessorService


def _make_service() -> tuple[PDFProcessorService, MagicMock, MagicMock]:
    db = MagicMock()
    db.add_rule_module = AsyncMock(return_value=None)
    cache = MagicMock()
    cache.set_job_progress = AsyncMock(return_value=None)
    svc = PDFProcessorService(
        gemini_api_key="test-key",
        gemini_model="gemini-1.5-pro",
        chroma_host="localhost",
        chroma_port=8000,
    )
    return svc, db, cache


# ── Sliding-window chunking ───────────────────────────────────────────────────

def test_sliding_window_chunks_basic():
    svc, _, _ = _make_service()
    text = "A" * 1000
    chunks = svc._sliding_window_chunks(text)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk["text"]) <= 810  # _CHUNK_SIZE=800 with slight tolerance


def test_sliding_window_chunks_empty():
    svc, _, _ = _make_service()
    assert svc._sliding_window_chunks("") == []


def test_sliding_window_chunks_short_text():
    svc, _, _ = _make_service()
    text = "Short"
    chunks = svc._sliding_window_chunks(text)
    assert len(chunks) == 1
    assert chunks[0]["text"] == "Short"


def test_sliding_window_chunks_overlap():
    svc, _, _ = _make_service()
    # 900-char text: fits one 800-char chunk, then a 220-char tail from offset 680
    text = "B" * 900
    chunks = svc._sliding_window_chunks(text)
    assert len(chunks) == 2
    # Second chunk starts at step=680
    assert chunks[1]["text"] == text[680:]


def test_sliding_window_chunks_unique_ids():
    svc, _, _ = _make_service()
    text = "C" * 2000
    chunks = svc._sliding_window_chunks(text)
    ids = [c["id"] for c in chunks]
    assert len(ids) == len(set(ids))


# ── ingest_pdf pipeline ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_pdf_success():
    svc, db, cache = _make_service()

    mock_page = MagicMock()
    mock_page.get_text.return_value = "Rules text " * 20  # well above 30-char threshold

    mock_doc = MagicMock()
    mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))
    mock_doc.__len__ = MagicMock(return_value=1)

    mock_chroma_client = AsyncMock()
    mock_collection = AsyncMock()
    mock_chroma_client.get_or_create_collection.return_value = mock_collection

    mock_embed_fn = MagicMock()

    with (
        patch("fitz.open", return_value=mock_doc),
        patch("chromadb.AsyncHttpClient", new_callable=AsyncMock, return_value=mock_chroma_client),
        patch(
            "chromadb.utils.embedding_functions.GoogleGenerativeAiEmbeddingFunction",
            return_value=mock_embed_fn,
        ),
    ):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            pdf_path = pathlib.Path(f.name)

        await svc.ingest_pdf(
            pdf_path=pdf_path,
            campaign_id="camp-1",
            module_name="Test Module",
            job_id="job-1",
            db=db,
            cache=cache,
        )

    last_progress = cache.set_job_progress.call_args_list[-1][0][1]
    assert last_progress["status"] == "complete"
    db.add_rule_module.assert_called_once()


@pytest.mark.asyncio
async def test_ingest_pdf_gemini_fallback_on_sparse_page():
    """Pages with < 30 chars of extracted text trigger the Gemini Vision fallback."""
    svc, db, cache = _make_service()

    mock_page = MagicMock()
    mock_page.get_text.return_value = "tiny"  # 4 chars — below 30-char threshold
    mock_page.get_pixmap.return_value = MagicMock(tobytes=MagicMock(return_value=b"imgdata"))

    mock_doc = MagicMock()
    mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))
    mock_doc.__len__ = MagicMock(return_value=1)

    mock_chroma_client = AsyncMock()
    mock_collection = AsyncMock()
    mock_chroma_client.get_or_create_collection.return_value = mock_collection

    mock_embed_fn = MagicMock()

    mock_http_response = MagicMock()
    mock_http_response.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "Gemini extracted text " * 10}]}}]
    }
    mock_http_response.raise_for_status = MagicMock()

    mock_http_client = AsyncMock()
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=None)
    mock_http_client.post = AsyncMock(return_value=mock_http_response)

    with (
        patch("fitz.open", return_value=mock_doc),
        patch("chromadb.AsyncHttpClient", new_callable=AsyncMock, return_value=mock_chroma_client),
        patch(
            "chromadb.utils.embedding_functions.GoogleGenerativeAiEmbeddingFunction",
            return_value=mock_embed_fn,
        ),
        patch("httpx.AsyncClient", return_value=mock_http_client),
    ):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            pdf_path = pathlib.Path(f.name)

        await svc.ingest_pdf(
            pdf_path=pdf_path,
            campaign_id="camp-2",
            module_name="Vision Module",
            job_id="job-2",
            db=db,
            cache=cache,
        )

    mock_http_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_ingest_pdf_no_text_sets_error_status():
    """A PDF with no extractable text and failed vision fallback still terminates gracefully."""
    svc, db, cache = _make_service()

    mock_page = MagicMock()
    mock_page.get_text.return_value = ""
    mock_page.get_pixmap.return_value = MagicMock(tobytes=MagicMock(return_value=b"imgdata"))

    mock_doc = MagicMock()
    mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))
    mock_doc.__len__ = MagicMock(return_value=1)

    mock_chroma_client = AsyncMock()
    mock_collection = AsyncMock()
    mock_chroma_client.get_or_create_collection.return_value = mock_collection

    mock_embed_fn = MagicMock()

    mock_http_client = AsyncMock()
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=None)
    mock_http_client.post = AsyncMock(side_effect=Exception("vision API down"))

    with (
        patch("fitz.open", return_value=mock_doc),
        patch("chromadb.AsyncHttpClient", new_callable=AsyncMock, return_value=mock_chroma_client),
        patch(
            "chromadb.utils.embedding_functions.GoogleGenerativeAiEmbeddingFunction",
            return_value=mock_embed_fn,
        ),
        patch("httpx.AsyncClient", return_value=mock_http_client),
    ):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            pdf_path = pathlib.Path(f.name)

        await svc.ingest_pdf(
            pdf_path=pdf_path,
            campaign_id="camp-3",
            module_name="Empty Module",
            job_id="job-3",
            db=db,
            cache=cache,
        )

    statuses = [c[0][1]["status"] for c in cache.set_job_progress.call_args_list]
    assert "error" in statuses


@pytest.mark.asyncio
async def test_ingest_pdf_exception_sets_error_status():
    """An unexpected exception during ingestion must write an error status to cache."""
    svc, db, cache = _make_service()

    with patch("fitz.open", side_effect=RuntimeError("corrupt PDF")):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            pdf_path = pathlib.Path(f.name)

        await svc.ingest_pdf(
            pdf_path=pdf_path,
            campaign_id="camp-4",
            module_name="Corrupt Module",
            job_id="job-4",
            db=db,
            cache=cache,
        )

    statuses = [c[0][1]["status"] for c in cache.set_job_progress.call_args_list]
    assert "error" in statuses


# ── delete_collection ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_collection_success():
    svc, _, _ = _make_service()

    mock_chroma_client = AsyncMock()

    with patch("chromadb.AsyncHttpClient", new_callable=AsyncMock, return_value=mock_chroma_client):
        await svc.delete_collection("test-collection")

    mock_chroma_client.delete_collection.assert_called_once_with("test-collection")


@pytest.mark.asyncio
async def test_delete_collection_silences_chromadb_exceptions():
    svc, _, _ = _make_service()

    mock_chroma_client = AsyncMock()
    mock_chroma_client.delete_collection.side_effect = Exception("collection not found")

    with patch("chromadb.AsyncHttpClient", new_callable=AsyncMock, return_value=mock_chroma_client):
        # Must not raise — missing collection is silenced
        await svc.delete_collection("missing-collection")
