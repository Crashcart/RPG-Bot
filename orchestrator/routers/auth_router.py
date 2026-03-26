"""
Auth Router — White Portal Login & First-Boot Setup
=====================================================
Routes:
  GET  /web/setup   — First-boot admin account creation form
  POST /web/setup   — Create the initial admin account → redirect to dashboard
  GET  /web/login   — Login form
  POST /web/login   — Authenticate, write admin_id to session → redirect
  POST /web/logout  — Clear session → redirect to login

These routes are EXEMPT from the AuthGuardMiddleware (open paths).
The guard middleware explicitly allows /web/setup and /web/login through
without checking session or first-boot state.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger(__name__)
router = APIRouter()


def _tmpl(request: Request):
    return request.app.state.templates


def _auth(request: Request):
    return getattr(request.app.state, "auth", None)


# ── First-Boot Setup ──────────────────────────────────────────────────────────

@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    auth = _auth(request)
    if auth and not await auth.is_first_boot():
        # Already configured — send to login
        return RedirectResponse("/web/login", status_code=302)
    return _tmpl(request).TemplateResponse("setup.html", {"request": request})


@router.post("/setup")
async def setup_submit(
    request:  Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm:  str = Form(...),
):
    auth = _auth(request)
    if not auth:
        return RedirectResponse("/web/login", status_code=303)
    if not await auth.is_first_boot():
        return RedirectResponse("/web/login", status_code=303)

    error = None
    if len(username.strip()) < 3:
        error = "Username must be at least 3 characters."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif password != confirm:
        error = "Passwords do not match."

    if error:
        return _tmpl(request).TemplateResponse(
            "setup.html", {"request": request, "error": error}
        )

    ok = await auth.create_admin(username, password)
    if not ok:
        return _tmpl(request).TemplateResponse(
            "setup.html",
            {"request": request, "error": "Username already taken. Try another."},
        )

    # Auto-login after setup
    request.session["admin_id"] = username.strip().lower()
    logger.info("First-boot admin account created and auto-logged in: %s", username.strip().lower())
    return RedirectResponse("/web/", status_code=303)


# ── Login ─────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/web/"):
    if request.session.get("admin_id"):
        return RedirectResponse("/web/", status_code=302)
    return _tmpl(request).TemplateResponse(
        "login.html", {"request": request, "next": next}
    )


@router.post("/login")
async def login_submit(
    request:  Request,
    username: str = Form(...),
    password: str = Form(...),
    next:     str = Form("/web/"),
):
    auth = _auth(request)
    if not auth:
        return _tmpl(request).TemplateResponse(
            "login.html",
            {"request": request, "next": next, "error": "Auth service unavailable."},
        )

    ok = await auth.verify(username, password)
    if not ok:
        return _tmpl(request).TemplateResponse(
            "login.html",
            {"request": request, "next": next, "error": "Invalid username or password."},
        )

    request.session["admin_id"] = username.strip().lower()
    # Prevent open-redirect: only allow relative /web/ paths
    safe_next = next if (next.startswith("/web/") and not next.startswith("//")) else "/web/"
    return RedirectResponse(safe_next, status_code=303)


# ── Logout ────────────────────────────────────────────────────────────────────

@router.post("/logout")
async def logout(request: Request):
    admin_id = request.session.pop("admin_id", None)
    if admin_id:
        logger.info("Admin logged out: %s", admin_id)
    return RedirectResponse("/web/login", status_code=303)
