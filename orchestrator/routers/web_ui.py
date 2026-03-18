"""
Ironclad GM – Web UI Router
=============================
Serves HTML pages (Jinja2) for the browser-based rule management panel.
All routes are under /web/.  JSON API endpoints are under /api/.

Pages:
  GET  /web/              – Campaign dashboard
  GET  /web/rules         – Rule registry browser + upload form
  POST /web/rules/upload  – Load a new JSON rule module
  POST /web/rules/toggle/{id} – Toggle a module active/inactive
  POST /web/rules/delete/{id} – Remove a module
  GET  /web/lore          – Story memory / world facts browser
  GET  /web/log           – Action log browser
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)
router = APIRouter()

# Templates are loaded by main.py and injected via app.state
def _tmpl(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _db(request: Request):
    return request.app.state.db


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
# Lore Browser
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
