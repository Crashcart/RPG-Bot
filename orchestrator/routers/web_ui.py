"""
Ironclad GM – Web UI Router
=============================
Serves HTML pages (Jinja2) for the browser-based rule management panel.
All routes are under /web/.  JSON API endpoints are under /api/.

Pages:
  GET  /web/                       – Campaign dashboard
  GET  /web/rules                  – Rule registry browser + upload form
  POST /web/rules/upload           – Load a new JSON rule module
  POST /web/rules/toggle/{id}      – Toggle a module active/inactive
  POST /web/rules/delete/{id}      – Remove a module
  GET  /web/lore                   – Story memory / world facts browser (read)
  POST /web/lore/upsert            – Add or edit a story fact (write)
  POST /web/lore/delete            – Delete a story fact
  GET  /web/log                    – Action log browser
  GET  /web/nodes                  – AI node registry / Connection Dashboard
  POST /web/nodes/add              – Add or update an Ollama/Gemini node
  POST /web/nodes/toggle/{id}      – Enable / disable a node
  POST /web/nodes/delete/{id}      – Remove a node from the registry
  GET  /web/backchannel            – White Portal Admin Backchannel
  POST /web/backchannel/send       – Submit a new OOC directive
  POST /web/backchannel/cancel/{id} – Cancel a pending directive
  GET  /web/settings               – Runtime settings (channel map, AI models, API keys)
  POST /web/settings/general       – Save general + AI settings
  POST /web/settings/channels/add  – Add or update a channel map entry
  POST /web/settings/channels/delete – Remove a channel map entry
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

import re

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)
router = APIRouter()

_PDF_UPLOAD_DIR = Path(os.environ.get("PDF_UPLOAD_DIR", "/app/pdf_uploads"))
_PDF_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
_MAX_PDF_MB = 200  # reject uploads over this size


def _tmpl(request: Request) -> Jinja2Templates:
    return request.app.state.templates

def _db(request: Request):
    return request.app.state.db

def _cache(request: Request):
    return request.app.state.cache

def _pdf_processor(request: Request):
    return request.app.state.pdf_processor

def _backchannel(request: Request):
    return getattr(request.app.state, "backchannel", None)

def _telemetry(request: Request):
    return getattr(request.app.state, "telemetry", None)


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    db = _db(request)
    stats      = await db.get_dashboard_stats()
    campaigns  = await db.get_all_campaigns()
    recent     = await db.get_recent_actions(limit=8)
    return _tmpl(request).TemplateResponse("dashboard.html", {
        "request":        request,
        "page":           "dashboard",
        "stats":          stats,
        "campaigns":      campaigns,
        "recent_actions": recent,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Rule Registry
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/rules", response_class=HTMLResponse)
async def rules_page(request: Request, campaign_id: str = ""):
    db = _db(request)
    campaigns = await db.get_all_campaigns()
    modules   = await db.get_all_rule_modules(campaign_id) if campaign_id else []
    return _tmpl(request).TemplateResponse("rules.html", {
        "request":           request,
        "page":              "rules",
        "campaigns":         campaigns,
        "modules":           modules,
        "selected_campaign": campaign_id,
    })


@router.post("/rules/upload", response_class=RedirectResponse)
async def upload_rule_module(
    request: Request,
    campaign_id:       str  = Form(...),
    module_name:       str  = Form(...),
    module_type:       str  = Form("json"),
    module_data:       str  = Form("{}"),
    chroma_collection: str  = Form(""),
):
    db = _db(request)
    flash_key = "flash_ok"
    flash_msg = f"Module '{module_name}' loaded."
    try:
        data = json.loads(module_data) if module_type != "vector" else {}
        await db.add_rule_module(
            campaign_id=campaign_id,
            module_name=module_name,
            module_type=module_type,
            module_data=data,
            chroma_collection=chroma_collection or None,
        )
    except json.JSONDecodeError:
        flash_key = "flash_err"
        flash_msg = "Invalid JSON — check your rule data syntax."
    except Exception as exc:
        flash_key = "flash_err"
        flash_msg = str(exc)

    request.session[flash_key] = flash_msg
    return RedirectResponse(f"/web/rules?campaign_id={campaign_id}", status_code=303)


@router.post("/rules/toggle/{module_id}")
async def toggle_rule_module(request: Request, module_id: str, campaign_id: str = ""):
    db = _db(request)
    await db.toggle_rule_module(module_id)
    return {"ok": True}


@router.post("/rules/delete/{module_id}", response_class=RedirectResponse)
async def delete_rule_module(request: Request, module_id: str, campaign_id: str = ""):
    db = _db(request)
    await db.delete_rule_module(module_id)
    return RedirectResponse(f"/web/rules?campaign_id={campaign_id}", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Lore Browser – read + CRUD
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/lore", response_class=HTMLResponse)
async def lore_page(request: Request, campaign_id: str = "", entity_type: str = ""):
    db = _db(request)
    campaigns = await db.get_all_campaigns()
    facts     = await db.get_story_context(campaign_id, entity_type) if campaign_id else []
    return _tmpl(request).TemplateResponse("lore.html", {
        "request":           request,
        "page":              "lore",
        "campaigns":         campaigns,
        "facts":             facts,
        "selected_campaign": campaign_id,
        "entity_type":       entity_type,
    })


@router.post("/lore/upsert", response_class=RedirectResponse)
async def lore_upsert(
    request: Request,
    campaign_id: str = Form(...),
    entity_type: str = Form(...),
    entity_name: str = Form(...),
    summary:     str = Form(...),
    detail:      str = Form(""),
):
    db = _db(request)
    try:
        await db.upsert_story_fact(
            campaign_id=campaign_id,
            entity_type=entity_type,
            entity_name=entity_name.strip(),
            summary=summary.strip(),
            detail=detail.strip(),
        )
        request.session["flash_ok"] = f"Fact '{entity_name}' saved."
    except Exception as exc:
        request.session["flash_err"] = str(exc)
    return RedirectResponse(
        f"/web/lore?campaign_id={campaign_id}&entity_type={entity_type}", status_code=303
    )


@router.post("/lore/delete", response_class=RedirectResponse)
async def lore_delete(
    request: Request,
    campaign_id: str = Form(...),
    entity_type: str = Form(...),
    entity_name: str = Form(...),
):
    db = _db(request)
    try:
        await db.delete_story_fact(
            campaign_id=campaign_id,
            entity_type=entity_type,
            entity_name=entity_name,
        )
        request.session["flash_ok"] = f"Fact '{entity_name}' deleted."
    except Exception as exc:
        request.session["flash_err"] = str(exc)
    return RedirectResponse(
        f"/web/lore?campaign_id={campaign_id}&entity_type={entity_type}", status_code=303
    )


# ─────────────────────────────────────────────────────────────────────────────
# AI Node Registry — Connection Dashboard
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/nodes", response_class=HTMLResponse)
async def nodes_page(request: Request):
    db    = _db(request)
    nodes = await db.get_all_nodes()
    storyteller_enabled = await db.get_system_setting("storyteller_api_enabled", default=True)
    return _tmpl(request).TemplateResponse("nodes.html", {
        "request":             request,
        "page":                "nodes",
        "nodes":               nodes,
        "storyteller_enabled": bool(storyteller_enabled),
        "flash_ok":  request.session.pop("flash_ok",  ""),
        "flash_err": request.session.pop("flash_err", ""),
    })


@router.post("/nodes/storyteller-toggle", response_class=RedirectResponse)
async def nodes_storyteller_toggle(request: Request):
    """Flip the Cloud Storyteller (Gemini) on/off."""
    db = _db(request)
    current = await db.get_system_setting("storyteller_api_enabled", default=True)
    new_val = not bool(current)
    await db.set_system_setting("storyteller_api_enabled", new_val)
    label = "enabled" if new_val else "disabled (local fallback active)"
    request.session["flash_ok"] = f"Cloud Storyteller {label}."
    return RedirectResponse("/web/nodes", status_code=303)


@router.post("/nodes/add", response_class=RedirectResponse)
async def nodes_add(
    request:   Request,
    node_name: str = Form(...),
    node_type: str = Form("ollama"),
    host:      str = Form(...),
    model:     str = Form(""),
    priority:  int = Form(10),
    roles:     str = Form(""),   # comma-separated, e.g. "adjudication,narrative"
    notes:     str = Form(""),
):
    db = _db(request)
    # Parse roles: split on comma, strip whitespace, drop empties
    role_list = [r.strip().lower() for r in roles.split(",") if r.strip()]
    try:
        await db.upsert_node(
            node_name=node_name.strip(),
            node_type=node_type,
            host=host.strip().rstrip("/"),
            model=model.strip(),
            priority=priority,
            notes=notes.strip(),
            roles=role_list,
        )
        request.session["flash_ok"] = f"Node '{node_name}' saved."
    except Exception as exc:
        request.session["flash_err"] = str(exc)
    return RedirectResponse("/web/nodes", status_code=303)


@router.post("/nodes/toggle/{node_id}")
async def nodes_toggle(request: Request, node_id: str):
    db = _db(request)
    await db.toggle_node(node_id)
    return {"ok": True}


@router.post("/nodes/delete/{node_id}", response_class=RedirectResponse)
async def nodes_delete(request: Request, node_id: str):
    db = _db(request)
    try:
        await db.delete_node(node_id)
        request.session["flash_ok"] = "Node removed."
    except Exception as exc:
        request.session["flash_err"] = str(exc)
    return RedirectResponse("/web/nodes", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Action Log
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/log", response_class=HTMLResponse)
async def log_page(request: Request, campaign_id: str = "", outcome_filter: str = ""):
    db = _db(request)
    campaigns = await db.get_all_campaigns()
    entries   = await db.get_action_log(campaign_id, outcome_filter) if campaign_id else []
    return _tmpl(request).TemplateResponse("log.html", {
        "request":           request,
        "page":              "log",
        "campaigns":         campaigns,
        "entries":           entries,
        "selected_campaign": campaign_id,
        "outcome_filter":    outcome_filter,
    })


# ─────────────────────────────────────────────────────────────────────────────
# PDF Upload & Ingestion
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/rules/upload-pdf", response_class=RedirectResponse)
async def upload_pdf(
    request: Request,
    background_tasks: BackgroundTasks,
    campaign_id:  str        = Form(...),
    module_name:  str        = Form(...),
    pdf_file:     UploadFile = File(...),
):
    """
    Accept a PDF upload, save it to the uploads directory, and kick off a
    background ingestion job.  Redirects immediately to the rules page so the
    browser stays responsive; the client polls /web/rules/pdf-status/<job_id>
    for live progress.
    """
    if not pdf_file.filename or not pdf_file.filename.lower().endswith(".pdf"):
        request.session["flash_err"] = "Only PDF files are accepted."
        return RedirectResponse(f"/web/rules?campaign_id={campaign_id}", status_code=303)

    # Size guard — read first chunk to check content-length header
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > _MAX_PDF_MB * 1024 * 1024:
        request.session["flash_err"] = f"PDF exceeds the {_MAX_PDF_MB} MB limit."
        return RedirectResponse(f"/web/rules?campaign_id={campaign_id}", status_code=303)

    # Save to disk
    job_id   = str(uuid.uuid4())
    pdf_path = _PDF_UPLOAD_DIR / f"{job_id}.pdf"
    try:
        contents = await pdf_file.read()
        pdf_path.write_bytes(contents)
    except Exception as exc:
        request.session["flash_err"] = f"Upload failed: {exc}"
        return RedirectResponse(f"/web/rules?campaign_id={campaign_id}", status_code=303)

    # Fire background task — returns immediately
    processor = _pdf_processor(request)
    cache     = _cache(request)
    db        = _db(request)

    background_tasks.add_task(
        processor.ingest_pdf,
        pdf_path=pdf_path,
        campaign_id=campaign_id,
        module_name=module_name,
        job_id=job_id,
        db=db,
        cache=cache,
    )

    request.session["flash_ok"] = (
        f"'{module_name}' is being ingested in the background. "
        f"Job ID: {job_id[:8]}…"
    )
    return RedirectResponse(
        f"/web/rules?campaign_id={campaign_id}&job_id={job_id}", status_code=303
    )


@router.get("/rules/pdf-status/{job_id}", response_class=JSONResponse)
async def pdf_status(request: Request, job_id: str):
    """Polled by the browser every 2 s to show ingestion progress."""
    cache    = _cache(request)
    progress = await cache.get_job_progress(job_id)
    if not progress:
        return JSONResponse({"status": "unknown"}, status_code=404)
    return JSONResponse(progress)


# ─────────────────────────────────────────────────────────────────────────────
# Admin Backchannel – White Portal "God Mode" Interface
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/backchannel", response_class=HTMLResponse)
async def backchannel_page(request: Request, campaign_id: str = ""):
    db         = _db(request)
    bc         = _backchannel(request)
    campaigns  = await db.get_all_campaigns()
    directives = []
    if campaign_id and bc:
        directives = await bc.get_recent_directives(campaign_id, limit=50)
    return _tmpl(request).TemplateResponse("backchannel.html", {
        "request":           request,
        "page":              "backchannel",
        "campaigns":         campaigns,
        "directives":        directives,
        "selected_campaign": campaign_id,
        "flash_ok":          request.session.pop("flash_ok",  ""),
        "flash_err":         request.session.pop("flash_err", ""),
    })


@router.post("/backchannel/send", response_class=RedirectResponse)
async def backchannel_send(
    request:        Request,
    campaign_id:    str = Form(...),
    admin_id:       str = Form("web-admin"),
    directive_type: str = Form("scene_directive"),
    directive_text: str = Form(...),
    priority:       int = Form(5),
):
    bc = _backchannel(request)
    if not bc:
        request.session["flash_err"] = "Backchannel service unavailable."
        return RedirectResponse(f"/web/backchannel?campaign_id={campaign_id}", status_code=303)
    try:
        from orchestrator.schemas.payloads import DirectiveType, GMDirectiveRequest
        await bc.submit_directive(GMDirectiveRequest(
            campaign_id=campaign_id,
            admin_id=admin_id,
            directive_type=DirectiveType(directive_type),
            directive_text=directive_text.strip(),
            priority=max(1, min(10, priority)),
        ))
        request.session["flash_ok"] = "Directive queued — fires on the next player action."
    except Exception as exc:
        request.session["flash_err"] = str(exc)
    return RedirectResponse(f"/web/backchannel?campaign_id={campaign_id}", status_code=303)


@router.post("/backchannel/cancel/{directive_id}", response_class=JSONResponse)
async def backchannel_cancel(request: Request, directive_id: str):
    bc = _backchannel(request)
    if not bc:
        return JSONResponse({"ok": False, "error": "service unavailable"}, status_code=503)
    await bc.cancel_directive(directive_id)
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# Live Telemetry Terminal
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/telemetry", response_class=HTMLResponse)
async def telemetry_page(request: Request):
    telem = _telemetry(request)
    return _tmpl(request).TemplateResponse("telemetry.html", {
        "request":      request,
        "page":         "telemetry",
        "client_count": telem.client_count if telem else 0,
        "flash_ok":  request.session.pop("flash_ok",  ""),
        "flash_err": request.session.pop("flash_err", ""),
    })


# ─────────────────────────────────────────────────────────────────────────────
# GM Sandbox Chat
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/sandbox", response_class=HTMLResponse)
async def sandbox_page(request: Request):
    db        = _db(request)
    campaigns = await db.get_all_campaigns()
    return _tmpl(request).TemplateResponse("sandbox.html", {
        "request":   request,
        "page":      "sandbox",
        "campaigns": campaigns,
        "flash_ok":  request.session.pop("flash_ok",  ""),
        "flash_err": request.session.pop("flash_err", ""),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Settings Page
# ─────────────────────────────────────────────────────────────────────────────

_CHANNEL_KEY_RE = re.compile(r'^[a-z0-9_]{1,32}$')


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    db = _db(request)
    channel_map          = await db.get_system_setting("channel_map",           default={})
    admin_role_name      = await db.get_system_setting("admin_role_name",       default="GM")
    session_ttl          = await db.get_system_setting("session_ttl_seconds",   default=3600)
    gemini_model         = await db.get_system_setting("gemini_model",          default="gemini-1.5-pro")
    ollama_model         = await db.get_system_setting("ollama_model",          default="mistral:7b-instruct")
    claude_model         = await db.get_system_setting("claude_model",          default="claude-sonnet-4-6")
    cloud_provider       = await db.get_system_setting("cloud_provider",        default="gemini")
    adjudication_provider = await db.get_system_setting("adjudication_provider", default="ollama")
    gemini_api_key       = await db.get_system_setting("gemini_api_key",        default="")
    claude_api_key       = await db.get_system_setting("claude_api_key",        default="")
    # SillyTavern — external, not part of the stack install
    sillytavern_url      = await db.get_system_setting("sillytavern_url",       default="")
    sillytavern_model    = await db.get_system_setting("sillytavern_model",     default="")
    sillytavern_api_key  = await db.get_system_setting("sillytavern_api_key",   default="")
    return _tmpl(request).TemplateResponse("settings.html", {
        "request":               request,
        "page":                  "settings",
        "channel_map":           channel_map or {},
        "admin_role_name":       admin_role_name or "GM",
        "session_ttl":           session_ttl or 3600,
        "gemini_model":          gemini_model or "gemini-1.5-pro",
        "ollama_model":          ollama_model or "mistral:7b-instruct",
        "claude_model":          claude_model or "claude-sonnet-4-6",
        "cloud_provider":        cloud_provider or "gemini",
        "adjudication_provider": adjudication_provider or "ollama",
        "gemini_api_key_set":    bool(gemini_api_key),
        "claude_api_key_set":    bool(claude_api_key),
        # SillyTavern
        "sillytavern_url":        sillytavern_url or "",
        "sillytavern_model":      sillytavern_model or "",
        "sillytavern_api_key_set": bool(sillytavern_api_key),
        "sillytavern_connected":   bool(sillytavern_url),
        "flash_ok":  request.session.pop("flash_ok",  ""),
        "flash_err": request.session.pop("flash_err", ""),
    })


@router.post("/settings/general", response_class=RedirectResponse)
async def settings_general_save(
    request:               Request,
    admin_role_name:       str = Form("GM"),
    session_ttl:           int = Form(3600),
    gemini_model:          str = Form("gemini-1.5-pro"),
    ollama_model:          str = Form("mistral:7b-instruct"),
    claude_model:          str = Form("claude-sonnet-4-6"),
    cloud_provider:        str = Form("gemini"),
    adjudication_provider: str = Form("ollama"),
    gemini_api_key:        str = Form(""),
    claude_api_key:        str = Form(""),
    # SillyTavern fields — all optional, not part of the install
    sillytavern_url:       str = Form(""),
    sillytavern_model:     str = Form(""),
    sillytavern_api_key:   str = Form(""),
):
    db = _db(request)
    _VALID_CLOUD    = {"gemini", "claude", "sillytavern"}
    _VALID_ADJ      = {"ollama", "groq", "openrouter", "together", "sillytavern"}
    try:
        await db.set_system_setting("admin_role_name",     admin_role_name.strip() or "GM")
        await db.set_system_setting("session_ttl_seconds", max(60, session_ttl))
        await db.set_system_setting("gemini_model",        gemini_model.strip() or "gemini-1.5-pro")
        await db.set_system_setting("ollama_model",        ollama_model.strip() or "mistral:7b-instruct")
        await db.set_system_setting("claude_model",        claude_model.strip() or "claude-sonnet-4-6")
        if cloud_provider in _VALID_CLOUD:
            await db.set_system_setting("cloud_provider", cloud_provider)
        if adjudication_provider in _VALID_ADJ:
            await db.set_system_setting("adjudication_provider", adjudication_provider)
        # Only update API keys if the field is non-empty (blank = keep existing)
        if gemini_api_key.strip():
            await db.set_system_setting("gemini_api_key", gemini_api_key.strip())
        if claude_api_key.strip():
            await db.set_system_setting("claude_api_key", claude_api_key.strip())

        # SillyTavern — save unconditionally (blank URL = disabled)
        await db.set_system_setting("sillytavern_url",   sillytavern_url.strip())
        await db.set_system_setting("sillytavern_model", sillytavern_model.strip())
        if sillytavern_api_key.strip():
            await db.set_system_setting("sillytavern_api_key", sillytavern_api_key.strip())

        request.session["flash_ok"] = "Settings saved. API key and URL changes take effect after restart."
    except Exception as exc:
        logger.exception("Settings save failed: %s", exc)
        request.session["flash_err"] = str(exc)
    return RedirectResponse("/web/settings", status_code=303)


@router.post("/settings/channels/add", response_class=RedirectResponse)
async def settings_channels_add(
    request:     Request,
    channel_key: str = Form(...),
    channel_id:  str = Form(...),
):
    db = _db(request)
    key = channel_key.strip().lower()
    cid = channel_id.strip()
    if not _CHANNEL_KEY_RE.match(key):
        request.session["flash_err"] = (
            "Channel key must be 1–32 lowercase letters, digits, or underscores."
        )
        return RedirectResponse("/web/settings", status_code=303)
    if not cid.isdigit():
        request.session["flash_err"] = "Channel ID must be a numeric Discord snowflake."
        return RedirectResponse("/web/settings", status_code=303)
    try:
        channel_map = await db.get_system_setting("channel_map", default={}) or {}
        channel_map[key] = cid
        await db.set_system_setting("channel_map", channel_map)
        request.session["flash_ok"] = f"Channel '{key}' → {cid} saved. Takes effect within 60 s."
    except Exception as exc:
        logger.exception("Channel map add failed: %s", exc)
        request.session["flash_err"] = str(exc)
    return RedirectResponse("/web/settings", status_code=303)


@router.post("/settings/channels/delete", response_class=RedirectResponse)
async def settings_channels_delete(
    request:     Request,
    channel_key: str = Form(...),
):
    db = _db(request)
    try:
        channel_map = await db.get_system_setting("channel_map", default={}) or {}
        channel_map.pop(channel_key.strip(), None)
        await db.set_system_setting("channel_map", channel_map)
        request.session["flash_ok"] = f"Channel '{channel_key}' removed."
    except Exception as exc:
        logger.exception("Channel map delete failed: %s", exc)
        request.session["flash_err"] = str(exc)
    return RedirectResponse("/web/settings", status_code=303)
