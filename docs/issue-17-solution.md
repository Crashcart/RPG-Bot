# Issue #17 — Rulebook Ingestion API: List, Delete, Toggle

## Summary

Extends the existing PDF rulebook ingestion pipeline with three missing management endpoints (`GET /api/rulebook/list/{campaign_id}`, `DELETE /api/rulebook/{module_id}`, `PATCH /api/rulebook/{module_id}/toggle`), adds the `get_rule_module` DB helper needed by the delete endpoint, adds `delete_collection` to `PDFProcessorService` to clean up ChromaDB on deletion, and ships a pytest suite covering the core PDF processing logic.

## Context

Issue #17 was filed because `/api/rulebook/ingest` and `/api/rulebook/status/{job_id}` existed but there was no way for the Discord bot or web admin to:
- List which rulebooks are registered for a campaign
- Remove a rulebook module and its ChromaDB vector collection
- Temporarily disable a module without deleting it (toggle)

The `rule_registry` table and `PDFProcessorService` were already in place (PR for issue #6/#12 merged this infrastructure). What was missing was the management surface and test coverage.

## Approach

### 1. `DatabaseService.get_rule_module(module_id)` (`orchestrator/services/database.py`)

Added a single-row lookup by UUID to the Web UI section (after `get_all_rule_modules`). The delete endpoint needs this to retrieve the `chroma_collection` name before deleting the DB row, so ChromaDB cleanup can happen first.

### 2. `PDFProcessorService.delete_collection(collection_name)` (`orchestrator/services/pdf_processor.py`)

Added an async method that connects to ChromaDB and calls `chroma.delete_collection()`. Exceptions are caught and logged as warnings so a missing collection (e.g. if ChromaDB was wiped externally) does not block the DB delete.

### 3. Three new API endpoints (`orchestrator/main.py`)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/rulebook/list/{campaign_id}` | Returns all modules (active + inactive) for a campaign. Delegates to `db.get_all_rule_modules()` which already existed for the Web UI. |
| `DELETE` | `/api/rulebook/{module_id}` | Fetches the module row, drops its ChromaDB collection, then deletes the DB row. Returns `{deleted, module_id, module_name}`. |
| `PATCH` | `/api/rulebook/{module_id}/toggle` | Fetches the row, calls `db.toggle_rule_module()`, returns the new `active` state. The RAG ingestion phase only loads `active=TRUE` modules, so toggling is a zero-cost soft-disable. |

All three return 404 if `module_id` is not found.

### 4. Tests (`orchestrator/tests/test_pdf_processor.py`)

Added pytest-asyncio tests covering:
- `_sliding_window_chunks`: basic, empty, short text, overlap, unique IDs, whitespace-skipping
- `ingest_pdf`: happy path (mocked fitz + chromadb), Gemini Vision fallback (sparse page), no-text-extracted error path, exception error path
- `delete_collection`: successful deletion, exception silencing

All I/O (fitz, chromadb, httpx) is mocked via `unittest.mock.patch` so tests run without any external services.

### 5. `requirements-dev.txt`

Added `pytest` and `pytest-asyncio` (the only deps needed for the test suite; `asyncpg`, `chromadb`, `pymupdf`, etc. are already in `requirements.txt`).

## Testing

```bash
# Install dev deps
pip install -r orchestrator/requirements-dev.txt

# Run tests
pytest orchestrator/tests/test_pdf_processor.py -v
```

Expected output: all 11 tests pass (no DB or ChromaDB connection required).

## Assumptions

- `chroma_collection` may be an empty string `""` for JSON/raw modules that were registered without a vector collection. The delete endpoint skips `delete_collection` in that case.
- No DB migration is needed — this PR only queries and deletes from the existing `rule_registry` table; no schema changes.
- The Discord bot's `/rulebook` command can now call `GET /api/rulebook/list/{campaign_id}` to present a selection menu before calling `DELETE /api/rulebook/{module_id}`.
