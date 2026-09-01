"""
Unit tests for orchestrator/services/pdf_processor.py

Coverage:
  - _sliding_window_chunks (pure function)
  - PDFProcessorService.ingest_pdf (happy path, Gemini Vision, error paths)
  - PDFProcessorService._extract_page_via_gemini_vision
  - PDFProcessorService._extract_page_text_sync (static)
  - PDFProcessorService._render_page_sync (static)
  - PDFProcessorService._embed_and_store

Run:
  pip install -r requirements-dev.txt
  pytest orchestrator/tests/test_pdf_processor.py -v
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.pdf_processor import (
    PDFProcessorService,
    _CHUNK_OVERLAP,
    _CHUNK_SIZE,
    _MIN_TEXT_CHARS,
    _sliding_window_chunks,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_service() -> PDFProcessorService:
    return PDFProcessorService(
        gemini_api_key="test-key",
        gemini_model="gemini-1.5-pro",
        chroma_host="localhost",
        chroma_port=8000,
    )


def _mock_cache():
    cache = MagicMock()
    cache.set_job_progress = AsyncMock()
    return cache


def _mock_db():
    db = MagicMock()
    db.add_rule_module = AsyncMock()
    return db


def _fitz_doc(page_text: str, page_count: int = 1) -> MagicMock:
    mock_page = MagicMock()
    mock_page.get_text.return_value = page_text
    mock_page.deformation_matrix = MagicMock()
    mock_page.get_pixmap.return_value = MagicMock(tobytes=MagicMock(return_value=b"\x89PNG"))

    doc = MagicMock()
    doc.page_count = page_count
    doc.load_page.return_value = mock_page
    return doc


# ─────────────────────────────────────────────────────────────────────────────
# _sliding_window_chunks — pure function
# ─────────────────────────────────────────────────────────────────────────────

class TestSlidingWindowChunks:
    def test_empty_text_returns_empty(self):
        assert _sliding_window_chunks("", "book", 1) == []

    def test_whitespace_only_returns_empty(self):
        assert _sliding_window_chunks("   \n\n  \t  ", "book", 1) == []

    def test_short_text_produces_single_chunk(self):
        text = "Hello adventurer. You enter the tavern."
        chunks = _sliding_window_chunks(text, "PHB", 5)
        assert len(chunks) == 1
        assert chunks[0]["text"] == text
        assert chunks[0]["source"] == "PHB p.5"
        assert chunks[0]["page"] == 5

    def test_chunk_ids_are_unique_valid_uuids(self):
        text = "A" * (_CHUNK_SIZE * 3)
        chunks = _sliding_window_chunks(text, "book", 1)
        ids = [c["id"] for c in chunks]
        assert len(ids) == len(set(ids)), "Duplicate chunk IDs found"
        for id_ in ids:
            uuid.UUID(id_)  # raises ValueError if not a valid UUID

    def test_source_citation_format(self):
        chunks = _sliding_window_chunks("Some rules text.", "Mothership Core", 42)
        assert chunks[0]["source"] == "Mothership Core p.42"

    def test_long_text_produces_multiple_chunks(self):
        text = "X" * (_CHUNK_SIZE * 3)
        chunks = _sliding_window_chunks(text, "book", 1)
        assert len(chunks) > 2

    def test_each_chunk_no_longer_than_chunk_size(self):
        text = "Y" * (_CHUNK_SIZE * 4)
        for c in _sliding_window_chunks(text, "book", 1):
            assert len(c["text"]) <= _CHUNK_SIZE

    def test_excessive_newlines_collapsed_to_double(self):
        text = "Chapter 1\n\n\n\n\nIntroduction to rules"
        chunks = _sliding_window_chunks(text, "book", 1)
        assert "\n\n\n" not in chunks[0]["text"]

    def test_page_field_matches_argument(self):
        chunks = _sliding_window_chunks("Some text.", "book", 7)
        assert chunks[0]["page"] == 7

    def test_overlap_means_consecutive_chunks_share_content(self):
        text = "A" * _CHUNK_SIZE + "B" * _CHUNK_SIZE
        chunks = _sliding_window_chunks(text, "book", 1)
        assert len(chunks) >= 2
        tail = chunks[0]["text"][-_CHUNK_OVERLAP:]
        head = chunks[1]["text"][:_CHUNK_OVERLAP]
        assert tail == head


# ─────────────────────────────────────────────────────────────────────────────
# ingest_pdf — happy path
# ─────────────────────────────────────────────────────────────────────────────

class TestIngestPdfHappyPath:
    @pytest.mark.asyncio
    async def test_text_pdf_full_pipeline(self, tmp_path):
        svc = _make_service()
        cache = _mock_cache()
        db = _mock_db()

        pdf_file = tmp_path / "rulebook.pdf"
        pdf_file.write_bytes(b"fake pdf bytes")

        doc = _fitz_doc("A" * 200, page_count=2)

        with (
            patch("fitz.open", return_value=doc),
            patch.object(svc, "_embed_and_store", new_callable=AsyncMock) as mock_embed,
        ):
            await svc.ingest_pdf(
                pdf_path=pdf_file,
                campaign_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                module_name="Mothership",
                job_id="job-001",
                db=db,
                cache=cache,
            )

        mock_embed.assert_awaited_once()
        chunks_passed = mock_embed.call_args[0][0]
        assert len(chunks_passed) > 0

        db.add_rule_module.assert_awaited_once()
        kw = db.add_rule_module.call_args.kwargs
        assert kw["module_name"] == "Mothership"
        assert kw["module_type"] == "vector"
        assert kw["module_data"]["pages"] == 2

        final = cache.set_job_progress.call_args_list[-1][0][1]
        assert final["status"] == "complete"

    @pytest.mark.asyncio
    async def test_pdf_file_deleted_after_success(self, tmp_path):
        svc = _make_service()
        cache = _mock_cache()
        db = _mock_db()

        pdf_file = tmp_path / "r.pdf"
        pdf_file.write_bytes(b"x")

        with (
            patch("fitz.open", return_value=_fitz_doc("A" * 100)),
            patch.object(svc, "_embed_and_store", new_callable=AsyncMock),
        ):
            await svc.ingest_pdf(
                pdf_path=pdf_file,
                campaign_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                module_name="Book",
                job_id="job-002",
                db=db,
                cache=cache,
            )

        assert not pdf_file.exists()

    @pytest.mark.asyncio
    async def test_progress_reported_per_page(self, tmp_path):
        svc = _make_service()
        cache = _mock_cache()
        db = _mock_db()

        pdf_file = tmp_path / "r.pdf"
        pdf_file.write_bytes(b"x")

        doc = _fitz_doc("T" * 100, page_count=3)

        with (
            patch("fitz.open", return_value=doc),
            patch.object(svc, "_embed_and_store", new_callable=AsyncMock),
        ):
            await svc.ingest_pdf(
                pdf_path=pdf_file,
                campaign_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                module_name="DMG",
                job_id="job-003",
                db=db,
                cache=cache,
            )

        assert cache.set_job_progress.call_count >= 6

    @pytest.mark.asyncio
    async def test_collection_name_uses_campaign_and_job_prefix(self, tmp_path):
        svc = _make_service()
        cache = _mock_cache()
        db = _mock_db()

        pdf_file = tmp_path / "r.pdf"
        pdf_file.write_bytes(b"x")

        campaign_id = "abcdef12-0000-0000-0000-000000000000"
        job_id = "job12345-xxxx"

        captured: list[str] = []

        async def _capture(chunks, cname, *a, **kw):
            captured.append(cname)

        with (
            patch("fitz.open", return_value=_fitz_doc("A" * 100)),
            patch.object(svc, "_embed_and_store", side_effect=_capture),
        ):
            await svc.ingest_pdf(
                pdf_path=pdf_file,
                campaign_id=campaign_id,
                module_name="Book",
                job_id=job_id,
                db=db,
                cache=cache,
            )

        assert captured[0] == f"rules_{campaign_id[:8]}_{job_id[:8]}"


# ─────────────────────────────────────────────────────────────────────────────
# Gemini Vision fallback
# ─────────────────────────────────────────────────────────────────────────────

class TestGeminiVisionFallback:
    @pytest.mark.asyncio
    async def test_sparse_page_triggers_vision(self, tmp_path):
        svc = _make_service()
        cache = _mock_cache()
        db = _mock_db()

        pdf_file = tmp_path / "scanned.pdf"
        pdf_file.write_bytes(b"x")

        doc = _fitz_doc("a" * (_MIN_TEXT_CHARS - 1))

        with (
            patch("fitz.open", return_value=doc),
            patch.object(
                svc, "_extract_page_via_gemini_vision",
                new_callable=AsyncMock,
                return_value="OCR extracted text from scan.",
            ) as mock_vision,
            patch.object(svc, "_embed_and_store", new_callable=AsyncMock),
        ):
            await svc.ingest_pdf(
                pdf_path=pdf_file,
                campaign_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                module_name="Scanned",
                job_id="job-004",
                db=db,
                cache=cache,
            )

        mock_vision.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rich_page_skips_vision(self, tmp_path):
        svc = _make_service()
        cache = _mock_cache()
        db = _mock_db()

        pdf_file = tmp_path / "text.pdf"
        pdf_file.write_bytes(b"x")

        doc = _fitz_doc("A" * (_MIN_TEXT_CHARS + 50))

        with (
            patch("fitz.open", return_value=doc),
            patch.object(svc, "_extract_page_via_gemini_vision", new_callable=AsyncMock) as mock_vision,
            patch.object(svc, "_embed_and_store", new_callable=AsyncMock),
        ):
            await svc.ingest_pdf(
                pdf_path=pdf_file,
                campaign_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                module_name="TextBook",
                job_id="job-005",
                db=db,
                cache=cache,
            )

        mock_vision.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_vision_success_returns_extracted_text(self):
        svc = _make_service()
        doc = _fitz_doc("x")

        expected = "Extracted OCR text from the scanned page."
        gemini_resp = {
            "candidates": [{"content": {"parts": [{"text": expected}]}}]
        }

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = gemini_resp

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_client

            with patch.object(svc, "_render_page_sync", return_value=b"\x89PNG"):
                result = await svc._extract_page_via_gemini_vision(
                    doc, 0, asyncio.get_event_loop()
                )

        assert result == expected

    @pytest.mark.asyncio
    async def test_vision_render_failure_returns_empty(self):
        svc = _make_service()
        doc = _fitz_doc("x")

        with patch.object(svc, "_render_page_sync", side_effect=RuntimeError("render fail")):
            result = await svc._extract_page_via_gemini_vision(
                doc, 0, asyncio.get_event_loop()
            )

        assert result == ""

    @pytest.mark.asyncio
    async def test_vision_http_error_returns_empty(self):
        import httpx

        svc = _make_service()
        doc = _fitz_doc("x")

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500 Server Error", request=MagicMock(), response=MagicMock()
        )

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_client

            with patch.object(svc, "_render_page_sync", return_value=b"\x89PNG"):
                result = await svc._extract_page_via_gemini_vision(
                    doc, 0, asyncio.get_event_loop()
                )

        assert result == ""


# ─────────────────────────────────────────────────────────────────────────────
# ingest_pdf — error paths
# ─────────────────────────────────────────────────────────────────────────────

class TestIngestPdfErrorPaths:
    @pytest.mark.asyncio
    async def test_no_chunks_sets_error_status(self, tmp_path):
        svc = _make_service()
        cache = _mock_cache()
        db = _mock_db()

        pdf_file = tmp_path / "blank.pdf"
        pdf_file.write_bytes(b"x")

        with (
            patch("fitz.open", return_value=_fitz_doc("")),
            patch.object(
                svc, "_extract_page_via_gemini_vision",
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            await svc.ingest_pdf(
                pdf_path=pdf_file,
                campaign_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                module_name="Blank",
                job_id="job-err-1",
                db=db,
                cache=cache,
            )

        db.add_rule_module.assert_not_awaited()
        final = cache.set_job_progress.call_args_list[-1][0][1]
        assert final["status"] == "error"
        assert "No text" in final["error"]

    @pytest.mark.asyncio
    async def test_fitz_open_exception_sets_error_status(self, tmp_path):
        svc = _make_service()
        cache = _mock_cache()
        db = _mock_db()

        pdf_file = tmp_path / "corrupt.pdf"
        pdf_file.write_bytes(b"not a pdf")

        with patch("fitz.open", side_effect=RuntimeError("corrupt file")):
            await svc.ingest_pdf(
                pdf_path=pdf_file,
                campaign_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                module_name="Corrupt",
                job_id="job-err-2",
                db=db,
                cache=cache,
            )

        final = cache.set_job_progress.call_args_list[-1][0][1]
        assert final["status"] == "error"
        assert "corrupt file" in final["error"]

    @pytest.mark.asyncio
    async def test_pdf_file_deleted_even_on_error(self, tmp_path):
        svc = _make_service()
        cache = _mock_cache()
        db = _mock_db()

        pdf_file = tmp_path / "error.pdf"
        pdf_file.write_bytes(b"x")

        with patch("fitz.open", side_effect=RuntimeError("boom")):
            await svc.ingest_pdf(
                pdf_path=pdf_file,
                campaign_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                module_name="Test",
                job_id="job-err-3",
                db=db,
                cache=cache,
            )

        assert not pdf_file.exists()

    @pytest.mark.asyncio
    async def test_embed_failure_sets_error_status(self, tmp_path):
        svc = _make_service()
        cache = _mock_cache()
        db = _mock_db()

        pdf_file = tmp_path / "r.pdf"
        pdf_file.write_bytes(b"x")

        with (
            patch("fitz.open", return_value=_fitz_doc("A" * 200)),
            patch.object(
                svc, "_embed_and_store",
                new_callable=AsyncMock,
                side_effect=RuntimeError("chroma unavailable"),
            ),
        ):
            await svc.ingest_pdf(
                pdf_path=pdf_file,
                campaign_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                module_name="Rules",
                job_id="job-err-4",
                db=db,
                cache=cache,
            )

        db.add_rule_module.assert_not_awaited()
        final = cache.set_job_progress.call_args_list[-1][0][1]
        assert final["status"] == "error"
        assert "chroma unavailable" in final["error"]

    @pytest.mark.asyncio
    async def test_error_message_truncated_to_300_chars(self, tmp_path):
        svc = _make_service()
        cache = _mock_cache()
        db = _mock_db()

        pdf_file = tmp_path / "r.pdf"
        pdf_file.write_bytes(b"x")

        long_msg = "E" * 500

        with patch("fitz.open", side_effect=RuntimeError(long_msg)):
            await svc.ingest_pdf(
                pdf_path=pdf_file,
                campaign_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                module_name="Test",
                job_id="job-err-5",
                db=db,
                cache=cache,
            )

        final = cache.set_job_progress.call_args_list[-1][0][1]
        assert len(final["error"]) <= 300


# ─────────────────────────────────────────────────────────────────────────────
# _extract_page_text_sync (static)
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractPageTextSync:
    def test_calls_load_page_and_get_text(self):
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Chapter 1 content"

        doc = MagicMock()
        doc.load_page.return_value = mock_page

        result = PDFProcessorService._extract_page_text_sync(doc, 0)

        doc.load_page.assert_called_once_with(0)
        mock_page.get_text.assert_called_once_with("text")
        assert result == "Chapter 1 content"

    def test_correct_page_number_forwarded(self):
        mock_page = MagicMock()
        mock_page.get_text.return_value = ""

        doc = MagicMock()
        doc.load_page.return_value = mock_page

        PDFProcessorService._extract_page_text_sync(doc, 9)
        doc.load_page.assert_called_once_with(9)


# ─────────────────────────────────────────────────────────────────────────────
# _embed_and_store
# ─────────────────────────────────────────────────────────────────────────────

def _make_chunks(n: int) -> list[dict]:
    return [
        {"id": str(uuid.uuid4()), "text": f"Rule {i}", "source": "book p.1", "page": 1}
        for i in range(n)
    ]


class TestEmbedAndStore:
    @pytest.mark.asyncio
    async def test_upsert_called_once_per_batch(self):
        svc = _make_service()
        cache = _mock_cache()
        chunks = _make_chunks(7)

        mock_collection = AsyncMock()
        mock_chroma = AsyncMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)

        with (
            patch("chromadb.AsyncHttpClient", new_callable=AsyncMock, return_value=mock_chroma),
            patch(
                "chromadb.utils.embedding_functions.GoogleGenerativeAiEmbeddingFunction",
                return_value=MagicMock(),
            ),
        ):
            await svc._embed_and_store(chunks, "col-a", "job-x", cache, batch_size=3)

        assert mock_collection.upsert.call_count == 3

    @pytest.mark.asyncio
    async def test_collection_created_with_cosine_metadata(self):
        svc = _make_service()
        cache = _mock_cache()

        mock_collection = AsyncMock()
        mock_chroma = AsyncMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)

        with (
            patch("chromadb.AsyncHttpClient", new_callable=AsyncMock, return_value=mock_chroma),
            patch(
                "chromadb.utils.embedding_functions.GoogleGenerativeAiEmbeddingFunction",
                return_value=MagicMock(),
            ),
        ):
            await svc._embed_and_store(_make_chunks(1), "rules-col", "job-y", cache)

        kw = mock_chroma.get_or_create_collection.call_args.kwargs
        assert kw["name"] == "rules-col"
        assert kw["metadata"] == {"hnsw:space": "cosine"}

    @pytest.mark.asyncio
    async def test_embedding_progress_updated_each_batch(self):
        svc = _make_service()
        cache = _mock_cache()
        chunks = _make_chunks(10)

        mock_collection = AsyncMock()
        mock_chroma = AsyncMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)

        with (
            patch("chromadb.AsyncHttpClient", new_callable=AsyncMock, return_value=mock_chroma),
            patch(
                "chromadb.utils.embedding_functions.GoogleGenerativeAiEmbeddingFunction",
                return_value=MagicMock(),
            ),
        ):
            await svc._embed_and_store(chunks, "col-b", "job-z", cache, batch_size=5)

        embedding_calls = [
            c for c in cache.set_job_progress.call_args_list
            if c[0][1].get("status") == "embedding"
        ]
        assert len(embedding_calls) == 2

    @pytest.mark.asyncio
    async def test_upsert_receives_correct_ids_and_documents(self):
        svc = _make_service()
        cache = _mock_cache()
        chunks = _make_chunks(2)

        mock_collection = AsyncMock()
        mock_chroma = AsyncMock()
        mock_chroma.get_or_create_collection = AsyncMock(return_value=mock_collection)

        with (
            patch("chromadb.AsyncHttpClient", new_callable=AsyncMock, return_value=mock_chroma),
            patch(
                "chromadb.utils.embedding_functions.GoogleGenerativeAiEmbeddingFunction",
                return_value=MagicMock(),
            ),
        ):
            await svc._embed_and_store(chunks, "col-c", "job-w", cache)

        call_kw = mock_collection.upsert.call_args.kwargs
        assert call_kw["ids"] == [c["id"] for c in chunks]
        assert call_kw["documents"] == [c["text"] for c in chunks]


# ─────────────────────────────────────────────────────────────────────────────
# PDFProcessorService constructor
# ─────────────────────────────────────────────────────────────────────────────

class TestServiceConstructor:
    def test_attributes_stored(self):
        svc = PDFProcessorService(
            gemini_api_key="key-abc",
            gemini_model="gemini-1.5-flash",
            chroma_host="chromadb.internal",
            chroma_port=9000,
        )
        assert svc._gemini_api_key == "key-abc"
        assert svc._gemini_model == "gemini-1.5-flash"
        assert svc._chroma_host == "chromadb.internal"
        assert svc._chroma_port == 9000
