"""
Ironclad GM – Orchestrator Service
====================================
FastAPI application that drives the four-phase pipeline:
  Phase 1: Ingestion & Context Assembly
  Phase 2: Mechanical Adjudication (Ollama)
  Phase 3: State Commitment (PostgreSQL + Redis)
  Phase 4: Narrative Generation — GM Director (two-tier storyteller)
             Tier 1: GMDirector (Gemini or auto-promoted Ollama)
             Tier 2: SubAgentDispatcher → actor/scribe Ollama nodes
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.sessions import SessionMiddleware
from fastapi.templating import Jinja2Templates

from orchestrator.config import get_settings
from orchestrator.pipeline import (
    AdjudicationPhase,
    IngestionPhase,
    NarrationPhase,
    StateCommitPhase,
)
from orchestrator.routers import web_router
from orchestrator.schemas.payloads import (
    CampfireStatus,
    DowntimeSubmitRequest,
    DowntimeTaskStatus,
    IntentPayload,
    NarrativeResponsePayload,
    PipelineResult,
    PresenceUpdate,
    RecapRequest,
    RecapResponse,
    RetconRequest,
    RetconResponse,
)
from orchestrator.services import (
    CacheService,
    CampfireService,
    ChronicleService,
    DatabaseService,
    DowntimeService,
    GeminiClient,
    GMDirector,
    NodeRouter,
    OllamaClient,
    RAGService,
    RetconService,
    StoryMemoryService,
    SubAgentDispatcher,
)
from orchestrator.services.pdf_processor import PDFProcessorService

# ─────────────────────────────────────────────────────────────────────────────
settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

# Shared service instances (initialised in lifespan)
db            = DatabaseService(settings)
cache         = CacheService(settings)
rag           = RAGService(settings)
ollama        = OllamaClient(settings)   # env-default fallback
node_router   = NodeRouter(db, settings) # multi-node AI mesh
gemini        = GeminiClient(settings)
story_memory  = StoryMemoryService(settings)
pdf_processor = PDFProcessorService(
    gemini_api_key=settings.gemini_api_key,
    gemini_model=settings.gemini_model,
    chroma_host=settings.chroma_host,
    chroma_port=settings.chroma_port,
)

# ── Tier 2: Sub-Agent Dispatcher ─────────────────────────────────────────────
# Routes delegation tasks from the GM Director to actor/scribe Ollama nodes.
sub_agent_dispatcher = SubAgentDispatcher(node_router)

# ── Tier 1: GM Director (Central Storyteller) ─────────────────────────────────
# Selects the storyteller per-turn (Gemini or auto-promoted Ollama), runs the
# planning pass, dispatches sub-agents, synthesizes, and applies immersion filters.
gm_director = GMDirector(
    gemini=gemini,
    node_router=node_router,
    dispatcher=sub_agent_dispatcher,
    story_memory=story_memory,
)

# Pipeline phase singletons
ingestion    = IngestionPhase(db, rag)
adjudication = AdjudicationPhase(node_router)
state_commit = StateCommitPhase(db, cache)
narration    = NarrationPhase(gm_director)   # Phase 4 fully delegated to GMDirector

# ── Async Session Services (lazy-initialised in lifespan after pool is ready) ─
# These are bound to db.pool after connect(); placeholders set to None here.
chronicle: ChronicleService | None = None
campfire:  CampfireService  | None = None
downtime:  DowntimeService  | None = None
retcon:    RetconService     | None = None


async def _downtime_resolver_loop() -> None:
    """Background task: checks for overdue downtime tasks every 60 seconds."""
    import asyncio
    while True:
        await asyncio.sleep(60)
        try:
            if downtime:
                await downtime.resolve_pending()
        except Exception as exc:
            logger.error("Downtime resolver loop error: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    logger.info("Starting Ironclad GM Orchestrator…")
    await db.connect()
    await cache.connect()
    await rag.connect()
    await story_memory.connect(db.pool)
    await node_router.start()   # begin background health-check loop

    # Initialise async-session services now that db.pool is live
    global chronicle, campfire, downtime, retcon
    chronicle = ChronicleService(settings, db.pool)
    campfire  = CampfireService(settings, db.pool)
    downtime  = DowntimeService(settings, db.pool)
    retcon    = RetconService(db.pool)

    # Start downtime background resolver
    resolver_task = asyncio.create_task(_downtime_resolver_loop())

    yield

    logger.info("Shutting down Ironclad GM Orchestrator…")
    resolver_task.cancel()
    try:
        await resolver_task
    except asyncio.CancelledError:
        pass
    await node_router.stop()
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
    allow_methods=["POST", "GET", "PATCH", "DELETE"],
    allow_headers=["*"],
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    max_age=3600,
)

# ── Web UI setup ──────────────────────────────────────────────────────────────
_templates_dir = Path(__file__).parent / "templates"
app.state.templates    = Jinja2Templates(directory=str(_templates_dir))
app.state.db           = db
app.state.cache        = cache
app.state.pdf_processor = pdf_processor
app.state.node_router  = node_router

app.include_router(web_router, prefix="/web")


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

        # ── Campfire Mode Guard ────────────────────────────────────────────────
        # When key players are offline, deflect critical plot advances and
        # suggest downtime RP instead.  Soft-block: we still generate a
        # narrative, but the GM signals that the story clock is paused.
        if campfire and await campfire.is_campfire_active(intent.guild_id):
            cf_status = await campfire.get_status(intent.guild_id)
            absent_list = ", ".join(f"<@{p}>" for p in cf_status.absent_players[:3])
            return NarrativeResponsePayload(
                prompt_id=intent.intent_id,
                intent_id=intent.intent_id,
                narrative=(
                    "🔥 **Campfire Mode**\n\n"
                    "The fire crackles low. The story holds its breath.\n\n"
                    f"*{absent_list} {'is' if len(cf_status.absent_players) == 1 else 'are'} "
                    "currently away — the GM is pausing the main narrative clock "
                    "until the full party is present.*\n\n"
                    "**What you can do now:**\n"
                    "• Talk to each other around the fire (free RP — no dice, no consequences)\n"
                    "• Use `/downtime` to assign your character a background task\n"
                    "• Use `/recap` when your party-member returns to catch them up\n\n"
                    "_The story resumes the moment everyone is back._"
                ),
                embed_title="🔥 Campfire Mode — Story Paused",
                whisper=(
                    "Your instincts say now is not the time to push forward. "
                    "Wait for your allies."
                ),
            )
        campaign_id = campaign["id"]
        campaign_system = campaign["system"]

        # ── Phase 1: Ingestion & Context Assembly ─────────────────────────────
        context = await ingestion.assemble(intent, campaign_id)

        # ── Phase 2: Mechanical Adjudication ──────────────────────────────────
        resolution = await adjudication.resolve(context)

        # ── Phase 3: State Commitment ─────────────────────────────────────────
        commit = await state_commit.commit(resolution)

        # ── Phase 4: Narrative Generation (with story memory) ─────────────────
        narrative = await narration.narrate(
            resolution=resolution,
            commit=commit,
            character=context.character,
            player_intent=intent.raw_input,
            campaign_system=campaign_system,
            campaign_id=campaign_id,
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
# Rulebook API  (called by the Discord bot — JSON only, no sessions/redirects)
# ─────────────────────────────────────────────────────────────────────────────

_pdf_upload_dir = Path(os.environ.get("PDF_UPLOAD_DIR", "/app/pdf_uploads"))
_pdf_upload_dir.mkdir(parents=True, exist_ok=True)
_MAX_PDF_BYTES = 200 * 1024 * 1024  # 200 MB


@app.post("/api/rulebook/ingest", summary="Ingest a PDF rulebook (bot API)")
async def api_ingest_rulebook(
    background_tasks: BackgroundTasks,
    campaign_id: str        = Form(...),
    module_name: str        = Form(...),
    pdf_file:    UploadFile = File(...),
) -> dict:
    """
    Accepts a multipart PDF upload from the Discord bot (or any HTTP client).
    Saves the file, starts a background ingestion job, and returns the job ID
    immediately so the caller can poll /api/rulebook/status/{job_id}.
    """
    if not pdf_file.filename or not pdf_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    contents = await pdf_file.read()
    if len(contents) > _MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF exceeds the 200 MB limit.")

    import uuid as _uuid
    job_id   = str(_uuid.uuid4())
    pdf_path = _pdf_upload_dir / f"{job_id}.pdf"
    pdf_path.write_bytes(contents)

    background_tasks.add_task(
        pdf_processor.ingest_pdf,
        pdf_path=pdf_path,
        campaign_id=campaign_id,
        module_name=module_name,
        job_id=job_id,
        db=db,
        cache=cache,
    )

    logger.info("Bot-initiated PDF ingestion queued: job=%s module=%s", job_id, module_name)
    return {"job_id": job_id, "status": "queued", "module_name": module_name}


@app.get("/api/rulebook/status/{job_id}", summary="Poll PDF ingestion progress (bot API)")
async def api_rulebook_status(job_id: str) -> dict:
    progress = await cache.get_job_progress(job_id)
    if not progress:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    return progress


@app.get("/api/campaign/active", summary="Get the active campaign for a guild (bot API)")
async def api_active_campaign(guild_id: str) -> dict:
    campaign = await db.get_active_campaign(guild_id)
    if not campaign:
        raise HTTPException(status_code=404, detail=f"No active campaign for guild {guild_id}.")
    return campaign


# ─────────────────────────────────────────────────────────────────────────────
# Chronicle Recap API
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/api/recap",
    response_model=RecapResponse,
    summary="Generate a 'Previously on…' catch-up recap for an offline player",
)
async def api_recap(req: RecapRequest) -> RecapResponse:
    if not chronicle:
        raise HTTPException(status_code=503, detail="Chronicle service not initialised.")
    try:
        return await chronicle.generate_recap(req)
    except Exception as exc:
        logger.exception("Recap generation failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Presence / Campfire Mode API
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/api/presence",
    response_model=CampfireStatus,
    summary="Update a player's online/offline presence and recalculate campfire mode",
)
async def api_update_presence(update: PresenceUpdate) -> CampfireStatus:
    if not campfire:
        raise HTTPException(status_code=503, detail="Campfire service not initialised.")
    return await campfire.update_presence(update.player_id, update.guild_id, update.online)


@app.get(
    "/api/campfire/{guild_id}",
    response_model=CampfireStatus,
    summary="Get current campfire mode status for a guild",
)
async def api_campfire_status(guild_id: str) -> CampfireStatus:
    if not campfire:
        raise HTTPException(status_code=503, detail="Campfire service not initialised.")
    return await campfire.get_status(guild_id)


@app.post(
    "/api/campfire/{guild_id}/off",
    summary="Admin: manually disable campfire mode",
    status_code=200,
)
async def api_campfire_off(guild_id: str) -> dict:
    if not campfire:
        raise HTTPException(status_code=503, detail="Campfire service not initialised.")
    await campfire.force_campfire_off(guild_id)
    return {"status": "ok", "campfire_active": False}


# ─────────────────────────────────────────────────────────────────────────────
# Downtime Tasks API
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/api/downtime",
    response_model=DowntimeTaskStatus,
    summary="Submit an async downtime task for a player",
    status_code=201,
)
async def api_submit_downtime(req: DowntimeSubmitRequest) -> DowntimeTaskStatus:
    if not downtime:
        raise HTTPException(status_code=503, detail="Downtime service not initialised.")
    try:
        return await downtime.submit_task(req)
    except Exception as exc:
        logger.exception("Downtime submit failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get(
    "/api/downtime/notifications/{player_id}",
    summary="Poll for completed downtime task notifications (Discord bot polls this)",
)
async def api_downtime_notifications(player_id: str) -> list[dict]:
    if not downtime:
        raise HTTPException(status_code=503, detail="Downtime service not initialised.")
    notifications = await downtime.get_pending_notifications(player_id)
    return [n.model_dump() for n in notifications]


@app.patch(
    "/api/downtime/{task_id}/notified",
    summary="Mark a downtime task notification as delivered",
    status_code=200,
)
async def api_mark_notified(task_id: str) -> dict:
    if not downtime:
        raise HTTPException(status_code=503, detail="Downtime service not initialised.")
    await downtime.mark_notified(task_id)
    return {"status": "ok", "task_id": task_id}


# ─────────────────────────────────────────────────────────────────────────────
# Retcon API
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/api/retcon",
    response_model=RetconResponse,
    summary="Admin: roll back a specific action and restore pre-action character state",
)
async def api_retcon(req: RetconRequest) -> RetconResponse:
    if not retcon:
        raise HTTPException(status_code=503, detail="Retcon service not initialised.")
    try:
        return await retcon.apply_retcon(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Retcon failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Health Check
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health", summary="Health check")
async def health() -> dict:
    return {"status": "ok", "service": "ironclad-orchestrator"}
