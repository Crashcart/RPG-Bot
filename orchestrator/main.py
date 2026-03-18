"""
Ironclad GM – Orchestrator Service
====================================
FastAPI application that drives the four-phase pipeline:
  Phase 1: Ingestion & Context Assembly
  Phase 2: Mechanical Adjudication (Ollama)
  Phase 3: State Commitment (PostgreSQL + Redis)
  Phase 4: Narrative Generation (Gemini)
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from orchestrator.config import get_settings
from orchestrator.pipeline import (
    AdjudicationPhase,
    IngestionPhase,
    NarrationPhase,
    StateCommitPhase,
)
from orchestrator.schemas.payloads import IntentPayload, NarrativeResponsePayload, PipelineResult
from orchestrator.services import (
    CacheService,
    DatabaseService,
    GeminiClient,
    OllamaClient,
    RAGService,
)

# ─────────────────────────────────────────────────────────────────────────────
settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

# Shared service instances (initialised in lifespan)
db     = DatabaseService(settings)
cache  = CacheService(settings)
rag    = RAGService(settings)
ollama = OllamaClient(settings)
gemini = GeminiClient(settings)

# Pipeline phase singletons
ingestion    = IngestionPhase(db, rag)
adjudication = AdjudicationPhase(ollama)
state_commit = StateCommitPhase(db, cache)
narration    = NarrationPhase(gemini)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Ironclad GM Orchestrator…")
    await db.connect()
    await cache.connect()
    await rag.connect()
    yield
    logger.info("Shutting down Ironclad GM Orchestrator…")
    await db.disconnect()
    await cache.disconnect()


app = FastAPI(
    title="Ironclad GM Orchestrator",
    description="Four-phase TTRPG mechanical adjudication and narrative pipeline.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Primary Endpoint: Player Action
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/action",
    response_model=NarrativeResponsePayload,
    summary="Process a player action through the full pipeline",
)
async def process_action(intent: IntentPayload) -> NarrativeResponsePayload:
    """
    Entry point for Discord listener. Runs the four-phase pipeline and
    returns the final narrative response for display in the channel.
    """
    pipeline_start = time.monotonic()

    # ── Idempotency Guard ─────────────────────────────────────────────────────
    if not await cache.set_pipeline_lock(intent.intent_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Action {intent.intent_id} is already being processed.",
        )

    try:
        # ── Resolve active campaign ───────────────────────────────────────────
        campaign = await db.get_active_campaign(intent.guild_id)
        if not campaign:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No active campaign found for guild {intent.guild_id}.",
            )
        campaign_id = campaign["id"]
        campaign_system = campaign["system"]

        # ── Phase 1: Ingestion & Context Assembly ─────────────────────────────
        context = await ingestion.assemble(intent, campaign_id)

        # ── Phase 2: Mechanical Adjudication ──────────────────────────────────
        resolution = await adjudication.resolve(context)

        # ── Phase 3: State Commitment ─────────────────────────────────────────
        commit = await state_commit.commit(resolution)

        # ── Phase 4: Narrative Generation ─────────────────────────────────────
        narrative = await narration.narrate(
            resolution=resolution,
            commit=commit,
            character=context.character,
            player_intent=intent.raw_input,
            campaign_system=campaign_system,
        )

        # ── Persist audit log ─────────────────────────────────────────────────
        duration_ms = int((time.monotonic() - pipeline_start) * 1000)
        await db.log_action({
            "intent_id":          intent.intent_id,
            "campaign_id":        campaign_id,
            "character_id":       context.character.character_id,
            "player_id":          intent.player_id,
            "raw_input":          intent.raw_input,
            "intent_payload":     intent.model_dump(mode="json"),
            "mechanical_payload": resolution.model_dump(mode="json"),
            "state_delta":        commit.model_dump(mode="json"),
            "narrative_summary":  narrative.narrative[:500],
        })

        logger.info(
            "Pipeline complete for intent=%s in %dms (outcome=%s)",
            intent.intent_id,
            duration_ms,
            resolution.outcome.value,
        )

        return narrative

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Pipeline failed for intent %s: %s", intent.intent_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )
    finally:
        await cache.release_pipeline_lock(intent.intent_id)


# ─────────────────────────────────────────────────────────────────────────────
# Session Management
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/session", summary="Create or refresh a player session", status_code=201)
async def create_session(
    player_id: str,
    guild_id: str,
    channel_id: str,
    session_token: str,
    campaign_id: str | None = None,
    character_id: str | None = None,
) -> dict:
    await cache.create_session(
        session_token=session_token,
        player_id=player_id,
        guild_id=guild_id,
        channel_id=channel_id,
        campaign_id=campaign_id,
        character_id=character_id,
    )
    return {"status": "ok", "session_token": session_token}


# ─────────────────────────────────────────────────────────────────────────────
# Health Check
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health", summary="Health check")
async def health() -> dict:
    return {"status": "ok", "service": "ironclad-orchestrator"}
