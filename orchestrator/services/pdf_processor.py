"""
Ironclad GM – PDF Ingestion Service
=====================================
CPU-efficient pipeline: PDF → text → chunks → ChromaDB vector store.

Processing strategy (ordered by CPU cost, lowest first):
──────────────────────────────────────────────────────────
  1. PyMuPDF text extraction  — near-zero local CPU; handles the vast
                                majority of modern published rulebooks
                                (the PDF has embedded text).

  2. Gemini Vision fallback   — for scanned/image-only pages; zero local
                                CPU — offloads entirely to Google's servers.
                                Only invoked when a page yields < 30 chars
                                of extractable text.

Embeddings:
  ChromaDB is configured with Google's text-embedding-004 model so all
  vector computation happens server-side at Google — no local GPU/CPU load.

Background execution:
  All sync PyMuPDF calls run inside asyncio.get_event_loop().run_in_executor
  so they never block the FastAPI event loop.

Progress:
  Each job writes incremental progress to Redis so the browser can poll
  /web/rules/pdf-status/<job_id> for a live progress indicator.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Minimum extractable chars to consider a page "text-based"
_MIN_TEXT_CHARS = 30
# Chunking parameters
_CHUNK_SIZE    = 800   # characters per chunk
_CHUNK_OVERLAP = 120   # overlap between consecutive chunks

_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


# ─────────────────────────────────────────────────────────────────────────────
# Chunker
# ─────────────────────────────────────────────────────────────────────────────

def _sliding_window_chunks(
    text: str,
    source: str,
    page_num: int,
    chunk_size: int = _CHUNK_SIZE,
    overlap: int    = _CHUNK_OVERLAP,
) -> list[dict[str, Any]]:
    """
    Split text into overlapping windows.
    Each chunk carries its source citation for rulebook references.
    """
    # Collapse excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if not text:
        return []

    chunks = []
    step   = chunk_size - overlap
    start  = 0
    idx    = 0

    while start < len(text):
        end   = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append({
                "id":      str(uuid.uuid4()),
                "text":    chunk,
                "source":  f"{source} p.{page_num}",
                "page":    page_num,
            })
        start += step
        idx   += 1

    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# PDF Processor Service
# ─────────────────────────────────────────────────────────────────────────────

class PDFProcessorService:
    """
    Stateless helper — does not keep long-lived connections.
    Call `ingest_pdf()` from a FastAPI BackgroundTask.
    """

    def __init__(self, gemini_api_key: str, gemini_model: str, chroma_host: str, chroma_port: int) -> None:
        self._gemini_api_key = gemini_api_key
        self._gemini_model   = gemini_model
        self._chroma_host    = chroma_host
        self._chroma_port    = chroma_port

    # ── Public entry point ────────────────────────────────────────────────────

    async def ingest_pdf(
        self,
        pdf_path: Path,
        campaign_id: str,
        module_name: str,
        job_id: str,
        db,       # DatabaseService — passed to avoid circular import
        cache,    # CacheService
    ) -> None:
        """
        Full pipeline: extract → chunk → embed → store.
        Updates Redis job progress at each page.
        Registers the module in rule_registry on completion.
        """
        import fitz  # PyMuPDF — imported lazily so startup isn't affected

        collection_name = f"rules_{campaign_id[:8]}_{job_id[:8]}"

        try:
            await cache.set_job_progress(job_id, {
                "status": "extracting",
                "page": 0, "total": 0,
                "module_name": module_name,
                "collection": collection_name,
            })

            # ── Step 1: Open PDF in thread pool ───────────────────────────────
            loop = asyncio.get_event_loop()
            doc  = await loop.run_in_executor(None, fitz.open, str(pdf_path))
            total_pages = doc.page_count

            await cache.set_job_progress(job_id, {
                "status":      "extracting",
                "page":        0,
                "total":       total_pages,
                "module_name": module_name,
                "collection":  collection_name,
            })
            logger.info("PDF ingestion started: %s (%d pages)", module_name, total_pages)

            # ── Step 2: Extract text page by page ─────────────────────────────
            all_chunks: list[dict[str, Any]] = []

            for page_num in range(total_pages):
                page_text = await loop.run_in_executor(
                    None, self._extract_page_text_sync, doc, page_num
                )

                # Gemini Vision fallback for scanned pages
                if len(page_text.strip()) < _MIN_TEXT_CHARS:
                    logger.debug("Page %d: sparse text, using Gemini Vision.", page_num + 1)
                    page_text = await self._extract_page_via_gemini_vision(
                        doc, page_num, loop
                    )

                page_chunks = _sliding_window_chunks(
                    text=page_text,
                    source=module_name,
                    page_num=page_num + 1,
                )
                all_chunks.extend(page_chunks)

                # Progress update every page
                await cache.set_job_progress(job_id, {
                    "status":      "extracting",
                    "page":        page_num + 1,
                    "total":       total_pages,
                    "chunks_so_far": len(all_chunks),
                    "module_name": module_name,
                    "collection":  collection_name,
                })

            doc.close()
            logger.info(
                "Extraction complete: %d chunks from %d pages.", len(all_chunks), total_pages
            )

            if not all_chunks:
                await cache.set_job_progress(job_id, {
                    "status": "error",
                    "error":  "No text could be extracted from this PDF.",
                })
                return

            # ── Step 3: Embed and store in ChromaDB ───────────────────────────
            await cache.set_job_progress(job_id, {
                "status":      "embedding",
                "page":        total_pages,
                "total":       total_pages,
                "chunks":      len(all_chunks),
                "module_name": module_name,
                "collection":  collection_name,
            })

            await self._embed_and_store(all_chunks, collection_name, job_id, cache)

            # ── Step 4: Register module in rule_registry ──────────────────────
            await db.add_rule_module(
                campaign_id=campaign_id,
                module_name=module_name,
                module_type="vector",
                module_data={
                    "source_pdf":  pdf_path.name,
                    "pages":       total_pages,
                    "chunks":      len(all_chunks),
                },
                chroma_collection=collection_name,
            )

            await cache.set_job_progress(job_id, {
                "status":      "complete",
                "page":        total_pages,
                "total":       total_pages,
                "chunks":      len(all_chunks),
                "collection":  collection_name,
                "module_name": module_name,
            })
            logger.info("PDF ingestion complete: %s → collection %s", module_name, collection_name)

        except Exception as exc:
            logger.exception("PDF ingestion failed for job %s: %s", job_id, exc)
            await cache.set_job_progress(job_id, {
                "status": "error",
                "error":  str(exc)[:300],
            })
        finally:
            # Clean up the uploaded file once ingested
            try:
                pdf_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ── Text Extraction ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_page_text_sync(doc, page_num: int) -> str:
        """Synchronous PyMuPDF extraction — called inside run_in_executor."""
        page = doc.load_page(page_num)
        return page.get_text("text")  # type: ignore[attr-defined]

    async def _extract_page_via_gemini_vision(
        self, doc, page_num: int, loop: asyncio.AbstractEventLoop
    ) -> str:
        """
        Render the page to a PNG image, then send to Gemini Vision for OCR.
        Zero local CPU — all processing happens at Google's servers.
        """
        try:
            # Render at 150 DPI — low enough to save bandwidth, high enough for OCR
            pixmap_bytes = await loop.run_in_executor(
                None, self._render_page_sync, doc, page_num, 150
            )
            b64 = base64.b64encode(pixmap_bytes).decode()

            payload = {
                "contents": [{
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": b64,
                            }
                        },
                        {
                            "text": (
                                "This is a page from a tabletop role-playing game rulebook. "
                                "Extract all text exactly as written. "
                                "Preserve headings, tables, and lists. "
                                "Return only the extracted text, no commentary."
                            )
                        },
                    ]
                }],
                "generationConfig": {"temperature": 0.0, "maxOutputTokens": 4096},
            }

            url = (
                f"{_GEMINI_API_BASE}/{self._gemini_model}"
                f":generateContent?key={self._gemini_api_key}"
            )
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()

            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]

        except Exception as exc:
            logger.warning("Gemini Vision fallback failed for page %d: %s", page_num, exc)
            return ""

    @staticmethod
    def _render_page_sync(doc, page_num: int, dpi: int) -> bytes:
        """Render a PDF page to PNG bytes — called inside run_in_executor."""
        page   = doc.load_page(page_num)
        matrix = doc.load_page(page_num).deformation_matrix  # identity by default
        zoom   = dpi / 72.0
        import fitz
        mat    = fitz.Matrix(zoom, zoom)
        pix    = page.get_pixmap(matrix=mat, alpha=False)  # type: ignore[attr-defined]
        return pix.tobytes("png")

    # ── Embedding & Storage ───────────────────────────────────────────────────

    async def _embed_and_store(
        self,
        chunks: list[dict[str, Any]],
        collection_name: str,
        job_id: str,
        cache,
        batch_size: int = 50,
    ) -> None:
        """
        Embed chunks using Gemini text-embedding-004 (server-side, no local CPU)
        and upsert them into ChromaDB.  Batched to respect API rate limits.
        """
        import chromadb
        from chromadb.utils.embedding_functions import GoogleGenerativeAiEmbeddingFunction

        embed_fn = GoogleGenerativeAiEmbeddingFunction(
            api_key=self._gemini_api_key,
            model_name="models/text-embedding-004",
        )

        chroma = await chromadb.AsyncHttpClient(
            host=self._chroma_host,
            port=self._chroma_port,
        )
        collection = await chroma.get_or_create_collection(
            name=collection_name,
            embedding_function=embed_fn,  # type: ignore[arg-type]
            metadata={"hnsw:space": "cosine"},
        )

        total = len(chunks)
        for i in range(0, total, batch_size):
            batch = chunks[i : i + batch_size]
            await collection.upsert(
                ids       =[c["id"]   for c in batch],
                documents =[c["text"] for c in batch],
                metadatas =[{
                    "source":  c["source"],
                    "page":    c["page"],
                    "chunk_id": c["id"],
                } for c in batch],
            )
            # Update embedding progress
            await cache.set_job_progress(job_id, {
                "status":          "embedding",
                "chunks_embedded": i + len(batch),
                "chunks":          total,
                "collection":      collection_name,
            })
            logger.debug("Embedded batch %d/%d", i + len(batch), total)
