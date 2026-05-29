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
    ElevenLabsClient,
    FactionService,
    GeminiClient,
    GMDirector,
    HandoutService,
    ImageGenService,
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
from orchestrator.services.rolling_vault    import RollingVault
from orchestrator.services.sic              import SystemIntegrityCheck
from orchestrator.services.world_registry   import WorldRegistry
from orchestrator.services.pdf_processor    import PDFProcessorService
from orchestrator.schemas.world_schema      import WorldSchema, WorldSwitchRequest, WorldSwitchResponse

# placeholder — real content pushed separately
