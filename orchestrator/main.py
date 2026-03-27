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

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.sessions import SessionMiddleware
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

from orchestrator.config import get_settings
from orchestrator.pipeline import (
    AdjudicationPhase,
    IngestionPhase,
    NarrationPhase,
    StateCommitPhase,
)
from orchestrator.routers import auth_router, web_router
from orchestrator.schemas.payloads import (
    CampfireStatus,
    DirectiveType,
    DowntimeSubmitRequest,
    DowntimeTaskStatus,
    GMDirective,
    GMDirectiveRequest,
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
    AdminBackchannelService,
    AuthService,
    CacheService,
    CampfireService,
    ChronicleService,
    ClaudeClient,
    DatabaseService,
    DiskAgentService,
    DowntimeService,
    GeminiClient,
    GMDirector,
    NodeRouter,
    OllamaClient,
    RAGService,
    RetconService,
    SandboxService,
    StoryMemoryService,
    SubAgentDispatcher,
    TelemetryService,
    WebSearchService,
)
from orchestrator.services.janitor          import JanitorService
from orchestrator.services.paradox_engine   import ParadoxEngine
from orchestrator.services.prophetic_buffer import PropheticBuffer
from orchestrator.services.reality_wall     import RealityWall
from orchestrator.services.sic              import SICResult, SystemIntegrityCheck
from orchestrator.services.world_registry   import WorldRegistry
from orchestrator.services.pdf_processor    import PDFProcessorService
from orchestrator.schemas.world_schema      import WorldSchema, WorldSwitchRequest, WorldSwitchResponse

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
claude        = ClaudeClient(settings) if settings.claude_api_key else None
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

# ── Telemetry Service (no pool dependency — initialised immediately) ──────────
# Must be created before GMDirector so it can be injected.
telemetry_svc = TelemetryService()

# ── Web Search Service ────────────────────────────────────────────────────────
web_search = WebSearchService(settings)

# ── Disk Agent Service ────────────────────────────────────────────────────────
disk_agent = DiskAgentService(settings.world_data_dir)

# ── Reality Wall (SQLite world-state + path isolation) ────────────────────────
# TDR §2: vault DB at /app/data/vault/scribe_core.db
reality_wall = RealityWall(data_dir=settings.world_data_dir, vault_dir=settings.vault_dir)

# ── Paradox Engine (unreliable narrator post-processor) ───────────────────────
paradox_engine = ParadoxEngine()

# ── Prophetic Buffer (predictive asset pre-generation) ────────────────────────
_cloud_storyteller = claude if (settings.cloud_provider == "claude" and claude) else gemini
prophetic_buffer = PropheticBuffer(cache=cache, storyteller=_cloud_storyteller)

# ── Janitor (GFS backup + media auto-prune) ───────────────────────────────────
# Python JanitorService acts as secondary janitor; primary is the Alpine container
janitor = JanitorService(data_dir=settings.world_data_dir, backup_dir=settings.backups_dir)

# ── World Registry (dynamic genre discovery + schema cache) ───────────────────
world_registry = WorldRegistry(data_dir=settings.world_data_dir, reality_wall=reality_wall)

# ── System Integrity Check (SIC) ──────────────────────────────────────────────
# TDR §1: four-pillar verifier — runs on startup, on-demand, and post-backup.
sic = SystemIntegrityCheck(
    data_dir    = settings.world_data_dir,
    backups_dir = settings.backups_dir,
    ollama_host = settings.ollama_host,
    cache       = cache,
)
# Give janitor a reference so it runs SIC after each backup cycle.
janitor._sic = sic

# ── Tier 1: GM Director (Central Storyteller) ─────────────────────────────────
# Selects the storyteller per-turn (Gemini or auto-promoted Ollama), runs the
# planning pass, dispatches sub-agents, synthesizes, and applies immersion filters.
gm_director = GMDirector(
    gemini=gemini,
    node_router=node_router,
    dispatcher=sub_agent_dispatcher,
    story_memory=story_memory,
    telemetry=telemetry_svc,
    claude=claude,
    cloud_provider=settings.cloud_provider,
    reality_wall=reality_wall,
    paradox_engine=paradox_engine,
    world_registry=world_registry,
)

# Pipeline phase singletons
ingestion    = IngestionPhase(db, rag)
adjudication = AdjudicationPhase(node_router)
state_commit = StateCommitPhase(db, cache)
narration    = NarrationPhase(gm_director)   # Phase 4 fully delegated to GMDirector

# ── Async Session Services (lazy-initialised in lifespan after pool is ready) ─
# These are bound to db.pool after connect(); placeholders set to None here.
chronicle:   ChronicleService        | None = None
campfire:    CampfireService         | None = None
downtime:    DowntimeService         | None = None
retcon:      RetconService           | None = None
backchannel: AdminBackchannelService | None = None
auth:        AuthService             | None = None
sandbox:     SandboxService          | None = None


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
    await reality_wall.init()        # create SQLite schema + data dirs
    await world_registry.scan()      # discover all worlds in data/fonts/+templates/
    await prophetic_buffer.start()
    await janitor.start()

    # ── System Integrity Check (TDR §1) ──────────────────────────────────────
    # Inject cache reference now that cache is connected.
    sic._cache = cache
    sic_result = await sic.run()
    if sic_result.status == "critical":
        failed = [p for p in sic_result.pillars if not p.passed and p.critical]
        msgs   = "; ".join(p.message for p in failed)
        logger.critical(
            "SIC CRITICAL — bot connection aborted. Failures: %s", msgs
        )
        raise RuntimeError(f"System Integrity Check failed: {msgs}")

    # Initialise async-session services now that db.pool is live
    global chronicle, campfire, downtime, retcon, backchannel, auth, sandbox
    chronicle   = ChronicleService(settings, db.pool)
    campfire    = CampfireService(settings, db.pool)
    downtime    = DowntimeService(settings, db.pool)
    retcon      = RetconService(db.pool)
    backchannel = AdminBackchannelService(db.pool)
    auth        = AuthService(db.pool)
    sandbox     = SandboxService(
        gemini=gemini,
        node_router=node_router,
        story_memory=story_memory,
        web_search=web_search,
    )

    # Expose services to web router and middleware
    app.state.backchannel = backchannel
    app.state.telemetry   = telemetry_svc
    app.state.auth        = auth

    # Start downtime background resolver
    resolver_task = asyncio.create_task(_downtime_resolver_loop())

    yield

    logger.info("Shutting down Ironclad GM Orchestrator…")
    resolver_task.cancel()
    try:
        await resolver_task
    except asyncio.CancelledError:
        pass
    await prophetic_buffer.stop()
    await janitor.stop()
    await node_router.stop()
    await db.disconnect()
    await cache.disconnect()


# ─────────────────────────────────────────────────────────────────────────────
# Auth Guard Middleware
# ─────────────────────────────────────────────────────────────────────────────

_OPEN_WEB_PATHS = {"/web/login", "/web/setup"}


class AuthGuardMiddleware(BaseHTTPMiddleware):
    """
    Protect all /web/ routes behind session-based admin authentication.

    Middleware stack ordering (added first = innermost = executes last):
      AuthGuard → SessionMiddleware → CORSMiddleware → handler
    So by the time AuthGuard.dispatch() runs, request.session is already
    populated by SessionMiddleware.

    First-Boot: if admin_accounts is empty → force redirect to /web/setup.
    Normal: if session missing admin_id → redirect to /web/login.
    The open paths (/web/login, /web/setup) bypass auth entirely.
    """

    def __init__(self, app) -> None:
        super().__init__(app)
        # Once we confirm setup is complete, skip is_first_boot() DB calls.
        self._setup_done = False

    async def dispatch(self, request: StarletteRequest, call_next):
        path = request.url.path

        # Only guard /web/ routes
        if not path.startswith("/web/"):
            return await call_next(request)

        # Auth and setup pages are always open
        if path in _OPEN_WEB_PATHS:
            return await call_next(request)

        auth_svc = getattr(request.app.state, "auth", None)

        # First-boot check (cached after first successful login)
        if not self._setup_done and auth_svc:
            first_boot = await auth_svc.is_first_boot()
            if first_boot:
                return RedirectResponse("/web/setup", status_code=302)
            self._setup_done = True

        # Session auth check
        if not request.session.get("admin_id"):
            next_path = path
            return RedirectResponse(f"/web/login?next={next_path}", status_code=302)

        return await call_next(request)


app = FastAPI(
    title="Ironclad GM Orchestrator",
    description="Four-phase TTRPG mechanical adjudication and narrative pipeline.",
    version="1.0.0",
    lifespan=lifespan,
)

# Middleware stack (add_middleware builds LIFO — first added = innermost = executes last).
# Desired request order: SessionMiddleware → CORSMiddleware → AuthGuard → handler
# So add AuthGuard first (innermost), then CORS, then Session (outermost).
app.add_middleware(AuthGuardMiddleware)
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

app.include_router(auth_router, prefix="/web")
app.include_router(web_router,  prefix="/web")


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
        # Signal Health Sentinel: heavy AI task in flight
        _sentinel_key = "ironclad:sentinel:busy"
        try:
            await cache._redis.set(_sentinel_key, "adjudication", ex=60)
        except Exception:
            pass
        resolution = await adjudication.resolve(context)
        try:
            await cache._redis.delete(_sentinel_key)
        except Exception:
            pass

        # ── Phase 3: State Commitment ─────────────────────────────────────────
        commit = await state_commit.commit(resolution)

        # ── Fetch pending admin backchannel directives ────────────────────────
        active_directives: list[GMDirective] = []
        if backchannel:
            active_directives = await backchannel.get_pending_directives(campaign_id)

        # ── Phase 4: Narrative Generation (with story memory) ─────────────────
        narrative = await narration.narrate(
            resolution=resolution,
            commit=commit,
            character=context.character,
            player_intent=intent.raw_input,
            campaign_system=campaign_system,
            campaign_id=campaign_id,
            active_directives=active_directives or None,
        )

        # ── Consume injected directives ───────────────────────────────────────
        if backchannel and active_directives:
            await backchannel.consume_directives(
                directive_ids=[d.directive_id for d in active_directives],
                intent_id=intent.intent_id,
            )
            await telemetry_svc.emit(
                "directive_fired",
                count=len(active_directives),
                campaign_id=campaign_id,
                intent_id=intent.intent_id,
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

        await telemetry_svc.emit(
            "pipeline_complete",
            intent_id=intent.intent_id,
            duration_ms=duration_ms,
            outcome=resolution.outcome.value,
            campaign_id=campaign_id,
        )

        # ── Prophetic Buffer: fire-and-forget prefetch for next turn ──────────
        try:
            _pipeline_result = PipelineResult(
                intent=intent,
                resolution=resolution,
                commit=commit,
                narrative=narrative,
                pipeline_duration_ms=duration_ms,
            )
            await prophetic_buffer.enqueue(_pipeline_result)
        except Exception as _pb_exc:
            logger.debug("PropheticBuffer enqueue failed (non-fatal): %s", _pb_exc)

        # ── Ghost Continuity: cache for offline Discord bot delivery ──────────
        # Key expires in 1 hour; bot marks delivered via /api/narrative/delivered
        try:
            ghost_key = f"ghost:{intent.guild_id}:{intent.intent_id}"
            ghost_data = json.dumps({
                "intent_id":   intent.intent_id,
                "guild_id":    intent.guild_id,
                "channel_id":  intent.channel_id,
                "player_id":   intent.player_id,
                "narrative":   narrative.narrative,
                "embed_title": narrative.embed_title,
                "outcome":     resolution.outcome.value,
            })
            await cache._redis.set(ghost_key, ghost_data, ex=3600)
        except Exception as ghost_exc:
            logger.debug("Ghost Continuity cache write failed (non-fatal): %s", ghost_exc)

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
# Admin Backchannel API  (White Portal private interface)
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/api/backchannel/directive",
    response_model=GMDirective,
    summary="Submit an OOC World Architect directive to the GM Engine",
    status_code=201,
)
async def api_submit_directive(req: GMDirectiveRequest) -> GMDirective:
    """
    Submit a private admin instruction through the White Portal backchannel.
    The directive will be injected into the next player action's narrative
    for the specified campaign and then archived.

    Admin Discord accounts in the game channels are NOT elevated — this
    endpoint is the ONLY way to influence the story as a World Architect.
    """
    if not backchannel:
        raise HTTPException(status_code=503, detail="Backchannel service not initialised.")
    try:
        return await backchannel.submit_directive(req)
    except Exception as exc:
        logger.exception("Directive submission failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get(
    "/api/backchannel/directives/{campaign_id}",
    summary="List recent directives for a campaign (White Portal history view)",
)
async def api_list_directives(campaign_id: str, limit: int = 30) -> list[dict]:
    if not backchannel:
        raise HTTPException(status_code=503, detail="Backchannel service not initialised.")
    return await backchannel.get_recent_directives(campaign_id, limit)


@app.post(
    "/api/backchannel/directive/{directive_id}/cancel",
    summary="Cancel a pending directive before it fires",
    status_code=200,
)
async def api_cancel_directive(directive_id: str) -> dict:
    if not backchannel:
        raise HTTPException(status_code=503, detail="Backchannel service not initialised.")
    await backchannel.cancel_directive(directive_id)
    return {"status": "ok", "directive_id": directive_id}


# ─────────────────────────────────────────────────────────────────────────────
# GM Sandbox Chat API  (White Portal private testing interface)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/sandbox/chat", summary="Direct GM Engine / NPC chat (sandbox — no state changes)")
async def api_sandbox_chat(req: dict) -> dict:
    """
    Send a message directly to the GM Engine or a specific NPC persona.
    Supports optional web search grounding and image URL injection.
    No player state is modified.
    """
    if not sandbox:
        raise HTTPException(status_code=503, detail="Sandbox service not initialised.")
    try:
        return await sandbox.chat(
            message=req.get("message", ""),
            campaign_id=req.get("campaign_id") or "",
            persona=req.get("persona") or None,
            use_search=bool(req.get("use_search", False)),
            image_url=req.get("image_url") or None,
        )
    except Exception as exc:
        logger.exception("Sandbox chat failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/sandbox/upload-image", summary="Upload an image for sandbox visual analysis")
async def api_sandbox_upload_image(file: UploadFile = File(...)) -> dict:
    """
    Accept an image upload, save it to the PDF upload directory (reusing the
    same storage), and return a temporary URL the sandbox can pass to
    generate_with_image().
    """
    import uuid as _uuid
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are accepted.")
    contents = await file.read()
    if len(contents) > 20 * 1024 * 1024:  # 20 MB cap
        raise HTTPException(status_code=413, detail="Image exceeds 20 MB limit.")
    ext = file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else "jpg"
    img_id   = str(_uuid.uuid4())
    img_path = _pdf_upload_dir / f"{img_id}.{ext}"
    img_path.write_bytes(contents)
    return {"url": f"/api/sandbox/image/{img_id}.{ext}", "id": img_id}


@app.get("/api/sandbox/image/{filename}", summary="Serve an uploaded sandbox image")
async def api_sandbox_image(filename: str):
    """Serve a previously uploaded sandbox image for Gemini Vision analysis."""
    from fastapi.responses import FileResponse
    img_path = _pdf_upload_dir / filename
    if not img_path.exists() or ".." in filename:
        raise HTTPException(status_code=404, detail="Image not found.")
    return FileResponse(str(img_path))


# ─────────────────────────────────────────────────────────────────────────────
# Web Intel API  (standalone search endpoint)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/search", summary="Run a web search for GM fact-grounding")
async def api_web_search(q: str, max_results: int = 5) -> list[dict]:
    """Run a web search via WebSearchService and return structured results."""
    if not q.strip():
        return []
    return await web_search.search(q.strip(), max_results=min(max_results, 10))


# ─────────────────────────────────────────────────────────────────────────────
# Disk Agency API  (AI world artifact file system)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/disk/{campaign_id}/write", summary="Write a world artifact file (AI sandbox)")
async def api_disk_write(campaign_id: str, req: dict) -> dict:
    path    = req.get("path", "")
    content = req.get("content", "")
    if not path or not content:
        raise HTTPException(status_code=400, detail="path and content required.")
    try:
        return await disk_agent.write(campaign_id, path, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/disk/{campaign_id}/read", summary="Read a world artifact file")
async def api_disk_read(campaign_id: str, path: str) -> dict:
    try:
        content = await disk_agent.read(campaign_id, path)
        return {"path": path, "content": content}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/disk/{campaign_id}/list", summary="List world artifact files for a campaign")
async def api_disk_list(campaign_id: str, subdir: str = "") -> list[dict]:
    try:
        return await disk_agent.list_files(campaign_id, subdir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/disk/{campaign_id}/file", summary="Delete a world artifact file")
async def api_disk_delete(campaign_id: str, path: str) -> dict:
    try:
        deleted = await disk_agent.delete(campaign_id, path)
        return {"deleted": deleted, "path": path}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Visual Intel API  (image analysis via Gemini Vision)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/vision/analyse", summary="Analyse an image URL using Gemini Vision")
async def api_vision_analyse(req: dict) -> dict:
    """
    Analyse an image and return a rich textual description.
    Used by the Discord bot when a player attaches an image to /act.
    """
    image_url = req.get("image_url", "")
    context   = req.get("context", "Describe this image for a tabletop RPG Game Master.")
    if not image_url:
        raise HTTPException(status_code=400, detail="image_url required.")
    try:
        description = await gemini.generate_with_image(
            system_prompt=(
                "You are the visual intel module for an RPG Game Master. "
                "Describe the image in vivid, atmosphere-rich detail. "
                "Focus on narrative-relevant details: people, objects, environment, mood. "
                "Be concise — 2-4 sentences."
            ),
            user_prompt=context,
            image_url=image_url,
            max_tokens=200,
        )
        return {"description": description, "image_url": image_url}
    except Exception as exc:
        logger.exception("Vision analysis failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Ghost Continuity API  (Discord bot reconnect narrative delivery)
# ─────────────────────────────────────────────────────────────────────────────

@app.get(
    "/api/narrative/pending/{guild_id}",
    summary="Ghost Continuity: get any undelivered narratives for a guild",
)
async def api_pending_narratives(guild_id: str) -> list[dict]:
    """
    Called by the Discord bot on_ready to retrieve narratives that were
    generated while the bot was offline.  Returns up to 10 undelivered items.
    """
    import json as _json
    items = []
    try:
        # Scan Redis for keys matching the ghost continuity pattern
        pattern = f"ghost:{guild_id}:*"
        keys = await cache._redis.keys(pattern)
        for key in keys[:10]:
            raw = await cache._redis.get(key)
            if raw:
                try:
                    items.append(_json.loads(raw))
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("Ghost Continuity: pending fetch failed: %s", exc)
    return items


@app.post(
    "/api/narrative/{intent_id}/delivered",
    summary="Ghost Continuity: mark a narrative as delivered",
    status_code=200,
)
async def api_mark_narrative_delivered(intent_id: str, guild_id: str) -> dict:
    """Mark a ghost-continuity narrative as delivered so it is not re-sent."""
    try:
        key = f"ghost:{guild_id}:{intent_id}"
        await cache._redis.delete(key)
    except Exception as exc:
        logger.warning("Ghost Continuity: mark-delivered failed: %s", exc)
    return {"status": "ok", "intent_id": intent_id}


# ─────────────────────────────────────────────────────────────────────────────
# Live Telemetry WebSocket
# ─────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws/telemetry")
async def telemetry_websocket(websocket: WebSocket):
    """
    Real-time pipeline event stream for the White Portal telemetry terminal.
    Requires an authenticated admin session (session cookie must be present).
    Replays the last 200 events on connect, then streams live.
    """
    import asyncio

    # Auth check — session cookie must carry admin_id
    session = websocket.session if hasattr(websocket, "session") else {}
    if not session.get("admin_id"):
        await websocket.close(code=1008, reason="Unauthorized — admin session required")
        return

    if not telemetry_svc:
        await websocket.close(code=1011, reason="Telemetry service unavailable")
        return

    q = await telemetry_svc.connect(websocket)
    try:
        while True:
            event = await q.get()
            await websocket.send_text(json.dumps(event))
    except Exception:
        pass
    finally:
        telemetry_svc.disconnect(q)


# ─────────────────────────────────────────────────────────────────────────────
# Health Check
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health", summary="Health check")
async def health() -> dict:
    return {"status": "ok", "service": "ironclad-orchestrator"}


# ─────────────────────────────────────────────────────────────────────────────
# System Integrity Check (SIC) — TDR §1
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/api/sic/run",
    summary="Manually trigger a full System Integrity Check",
    tags=["sic"],
)
async def run_sic() -> dict:
    """
    Execute all four SIC pillars immediately.

    Execution triggers: startup (automatic), Director command (this endpoint),
    and Janitor post-backup (internal).  Results are cached in Redis and
    surfaced on the Pulse dashboard.
    """
    result = await sic.run()
    return result.to_dict()


@app.get(
    "/api/sic/status",
    summary="Return the last cached SIC result from Redis",
    tags=["sic"],
)
async def get_sic_status() -> dict:
    """
    Returns the most recent SIC result without re-running the checks.
    If no result is cached yet, triggers a fresh run.
    """
    try:
        raw = await cache.get("ironclad:sic:result")
        if raw:
            import json as _json
            return _json.loads(raw)
    except Exception:
        pass
    # No cached result — run now
    result = await sic.run()
    return result.to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# Dynamic World / Genre Orchestration
# ─────────────────────────────────────────────────────────────────────────────

@app.get(
    "/api/worlds",
    summary="List all discovered RPG worlds",
    response_model=list[WorldSchema],
)
async def list_worlds() -> list[WorldSchema]:
    """
    Return every world currently in the WorldRegistry cache.
    Each world corresponds to a subdirectory of data/fonts/.
    """
    return world_registry.list_worlds()


@app.post(
    "/api/world/switch",
    summary="Switch a campaign's active world (manifests new worlds automatically)",
    response_model=WorldSwitchResponse,
)
async def switch_world(req: WorldSwitchRequest) -> WorldSwitchResponse:
    """
    Bind a campaign to a world.

    - If the world folder exists in data/fonts/ it is activated immediately.
    - If it does not exist, the folder + minimal world.json are created
      on the fly (`manifested: true`) — no code changes required.

    Called by the Discord `/switch_world` slash command.
    """
    try:
        schema, manifested = await world_registry.switch_campaign_world(
            campaign_id=req.campaign_id,
            world_name=req.world_name,
        )
        return WorldSwitchResponse(
            campaign_id=req.campaign_id,
            world_name=req.world_name,
            manifested=manifested,
            schema=schema,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"World switch failed: {exc}",
        )


@app.get(
    "/api/world/{campaign_id}",
    summary="Get the active world schema for a campaign",
    response_model=WorldSchema,
)
async def get_campaign_world(campaign_id: str) -> WorldSchema:
    """Return the WorldSchema for the campaign's currently active world."""
    schema = await world_registry.get_campaign_schema(campaign_id)
    if schema is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No world set for campaign {campaign_id}.",
        )
    return schema
