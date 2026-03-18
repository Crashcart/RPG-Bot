"""
Ironclad GM – Media Asset Proxy
=================================
Lightweight FastAPI server that serves locally generated or cached
environmental images and sound cues to Discord via URL references.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(os.environ.get("ASSETS_DIR", "/app/assets"))
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Ironclad GM Media Proxy", version="1.0.0")

# Serve static assets at /assets/*
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "assets_dir": str(ASSETS_DIR)}


@app.get("/asset/{asset_path:path}", summary="Serve a named asset")
async def get_asset(asset_path: str) -> FileResponse:
    full_path = ASSETS_DIR / asset_path
    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(status_code=404, detail=f"Asset not found: {asset_path}")
    # Prevent path traversal
    try:
        full_path.resolve().relative_to(ASSETS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied.")
    return FileResponse(str(full_path))


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run(app, host="0.0.0.0", port=8001)
