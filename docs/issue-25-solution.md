# Issue #25 — Make & Take Campaign Module Exporter

## Summary

Implements a one-click campaign export pipeline that freezes world state into a portable `.tar.gz` archive.  The archive includes world lore, NPC data, action history, and optionally media assets, with player PII stripped before distribution.

## Architecture

```
Discord /export_campaign
      │
      ▼
POST /api/export/{campaign_id}
      │
      ▼
 ModuleExporter.start_export()          ← returns job_id immediately
      │  asyncio.create_task()
      ▼
 _run_export()                          ← background pipeline
  ├─ _extract_table()        story_facts, story_entities
  ├─ _extract_sanitized_table()  action_log (Discord IDs nulled)
  ├─ _extract_npc_only_table()  characters, inventories (NPCs only)
  ├─ _bundle_media()            handouts/ + echo_vault/ (if requested)
  ├─ _generate_manifest()       hardware tiers + table counts
  └─ tarfile.open("w:gz")       compress to /app/exports/modules/
      │
      ▼
GET /api/export/status/{job_id}        ← poll for completion
```

## New Files

| File | Purpose |
|------|---------|
| `db/migrations/014_module_exporter.sql` | `export_jobs` table (status, archive_path, manifest JSONB) |
| `orchestrator/schemas/exporter_schemas.py` | Pydantic models: `ExportJobStatus`, `HardwareTier`, `ExportManifest`, `ModuleExportRequest`, `ExportJobResponse` |
| `orchestrator/services/module_exporter.py` | `ModuleExporter` service — full export pipeline |
| `orchestrator/tests/test_module_exporter.py` | 18 pytest-asyncio unit tests (all mocked) |

## Database Schema

```sql
CREATE TABLE export_jobs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id   UUID REFERENCES campaigns(id) ON DELETE CASCADE,
    status        export_job_status NOT NULL DEFAULT 'queued',
    sanitized     BOOLEAN NOT NULL DEFAULT TRUE,
    include_media BOOLEAN NOT NULL DEFAULT TRUE,
    archive_path  TEXT,
    error_detail  TEXT,
    manifest      JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ
);
```

## Sanitization Rules

| Data | Treatment |
|------|-----------|
| `player_discord_id` column | Nulled |
| `user_id` column (non-campaign) | Nulled |
| 17–20 digit Discord snowflakes in text fields | Replaced with `[REDACTED]` |
| `sessions` table | Fully excluded |
| `admin_accounts` table | Fully excluded |
| `gm_directives` table | Fully excluded |
| `player_presence` table | Fully excluded |
| `characters` rows where `is_npc=FALSE` | Excluded (NPC rows only) |

## Manifest Format (`manifest.json`)

```json
{
  "schema_version": "1.0",
  "campaign_id": "...",
  "world_name": "mothership",
  "exported_at": "2026-06-17T00:00:00Z",
  "sanitized": true,
  "media_included": true,
  "hardware_tiers": [
    {"tier_name": "budget", "min_ram_gb": 4, "recommended_model": "phi3:3b-mini-4k-instruct-q4_K_M", "quantization": "Q4_K_M"},
    {"tier_name": "standard", "min_ram_gb": 8, "recommended_model": "mistral:7b-instruct-q4_K_M", "quantization": "Q4_K_M"},
    {"tier_name": "performance", "min_ram_gb": 16, "recommended_model": "mistral:7b-instruct", "quantization": "none"}
  ],
  "required_models": ["mistral:7b-instruct-q4_K_M"],
  "table_counts": {"story_facts": 47, "characters": 12, "action_log": 203},
  "media_file_count": 23,
  "archive_size_bytes": 10485760
}
```

## Wiring into main.py

```python
# In lifespan context manager:
module_exporter = ModuleExporter(
    db=db_service,
    export_dir=Path("/app/exports/modules"),
)

# Route handlers:
@app.post("/api/export/{campaign_id}")
async def start_export(campaign_id: str, include_media: bool = True, sanitize: bool = True):
    req = ModuleExportRequest(
        campaign_id=campaign_id,
        sanitize_player_data=sanitize,
        include_media=include_media,
    )
    return await module_exporter.start_export(req)

@app.get("/api/export/status/{job_id}")
async def export_status(job_id: str):
    return await module_exporter.get_export_status(job_id)
```

## TDR Compliance

| TDR Requirement | Implementation |
|-----------------|----------------|
| One-click export | `POST /api/export/{campaign_id}` enqueues background task |
| Campaign DB freeze (read-only snapshot) | asyncio background task reads; no writes to campaign tables |
| ChromaDB lore export | `story_facts` / `story_entities` tables captured |
| Asset bundling | `_bundle_media()` copies `handouts/` + `echo_vault/` |
| Manifest.json with hardware tiers | `_generate_manifest()` writes budget/standard/performance tiers |
| Player data sanitization | `_sanitize_record()` nulls discord IDs, redacts snowflakes |
| FOSS tooling only | stdlib `tarfile` + `shutil` — zero extra dependencies |
| UUID path traversal protection | `validate_campaign_id()` enforces UUID4 regex before any FS op |

## Post-merge steps

1. Run `db/migrations/014_module_exporter.sql` on staging/production PostgreSQL
2. Add `module_exporter = ModuleExporter(db=db_service)` to `main.py` lifespan
3. Register the two route handlers (`/api/export/{campaign_id}` + `/api/export/status/{job_id}`)
4. Mount an `exports` volume in `docker-compose.yml` → `/app/exports`
